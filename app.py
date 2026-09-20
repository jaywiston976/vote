import io
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from sqlalchemy.exc import IntegrityError

from config import Config
from models import Ballot, Poll, PollOption, User, Voter, db

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

with app.app_context():
    # 表结构不存在时自动补齐（幂等，不会改动已存在的表，也不会清空任何数据）
    db.create_all()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("请先登录", "warn")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """仅 admin 账号可访问发起 / 管理投票相关功能。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("请先登录", "warn")
            return redirect(url_for("login", next=request.path))
        if not is_admin(current_user()):
            flash("只有管理员可以发起和管理投票", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


@app.context_processor
def inject_user():
    user = current_user()
    return {"current_user": user, "is_admin": is_admin(user)}


@app.after_request
def add_no_cache_headers(response):
    """禁止浏览器 / 中间代理 / CDN 缓存动态页面。

    多人同时在线时“自动登录成别人账号”的典型根因，是带有 Set-Cookie
    的登录 / 已登录页被共享缓存后原样发给了其他访客。这里对所有非静态
    资源强制 no-store，确保每个用户拿到的都是属于自己会话的响应。
    """
    if request.path.startswith("/static/"):
        return response
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    # 明确告知代理：响应随 Cookie 变化，不可跨用户复用
    response.headers["Vary"] = "Cookie"
    return response


# ---------------------------------------------------------------------------
# 密码校验
# ---------------------------------------------------------------------------
# 允许使用弱密码（不再拦截常用/简单密码），仅在页面上给出提示。
# 唯一的硬性限制：密码只能由大小写字母和数字组成，三类字符任选其一即可
# （即不强制同时包含三类，但不允许出现其他字符，如符号、空格、中文等）。
def validate_password_charset(password: str):
    """校验密码字符集，返回错误提示字符串；通过则返回 None。"""
    if not password:
        return "密码不能为空"
    if not re.fullmatch(r"[A-Za-z0-9]+", password):
        return "密码只能使用大小写字母和数字"
    return None


# 唯一允许发起 / 管理投票的账号：用真实姓名 "admin" 登录。
#
# 密码来源优先级：
#   1. 环境变量 ADMIN_PASSWORD（推荐：在 Render 面板改，改完自动重新部署，不用动代码）
#   2. 下面的默认值
# 注意：这个常量是 ensure_admin_account() 每次启动「强制校正」的目标值，
# 所以直接去数据库改 admin 的密码是无效的，下次启动/唤醒会被覆盖回来。
ADMIN_NAME = "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "muyu123")

# ---------------------------------------------------------------------------
# 字段长度上限（必须与 models.py 的列定义一致）
# ---------------------------------------------------------------------------
# 这些校验不是装饰：PostgreSQL 的 varchar(n) 遇到超长值不会截断，而是直接抛
# DataError，页面变成 500。线上实测确认过 5 处会打成 500：
# 超长姓名、超长密码、超长投票标题、超长选项名、名单里的超长姓名。
MAX_NAME_LEN = 64       # users.real_name / users.username / voters.name
MAX_TITLE_LEN = 200     # polls.title
MAX_OPTION_LEN = 200    # poll_options.name
MAX_PASSWORD_LEN = 128  # users.password_hash（明文前缀 + 密码）


def filter_long_names(names, label="姓名"):
    """剔除超长姓名（上限 MAX_NAME_LEN），返回 (保留的, 被剔除的)。

    名单可能来自粘贴或 Excel 导入，里面混进整段备注文字是常见情况，
    不处理就会把整场投票创建打成 500。
    """
    ok, dropped = [], []
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        (ok if len(n) <= MAX_NAME_LEN else dropped).append(n)
    return ok, dropped


def is_admin(user) -> bool:
    """仅 admin 这一特定账号可以发起 / 管理投票。

    以真实姓名识别为主；同时兼容旧库中 username='admin' 而真实姓名仍是
    「管理员」的历史行，避免升级后管理员权限凭空丢失。普通用户注册时
    real_name == 'admin' 会被拒绝，且 username 恒等于 real_name，
    因此 username=='admin' 只可能是管理员账号，不会误判。
    """
    if not user:
        return False
    return ADMIN_NAME in (user.real_name, user.username)


def ensure_admin_account():
    """确保存在唯一的管理员账号：真实姓名 admin / 密码 ADMIN_PASSWORD。

    抗并发：gunicorn 多 worker 会同时执行本函数，若都判定 admin 不存在
    并各自 INSERT，会触发唯一约束冲突。这里捕获 IntegrityError 回滚重查，
    保证任意 worker、任意次数执行都安全且结果一致。
    每次启动都把 admin 密码校正为 ADMIN_PASSWORD，避免旧库里的旧密码残留。

    兼容旧库：早期版本按 username 识别管理员，admin 行的 real_name 可能是
    「管理员」/「系统管理员」。登录方式改为「真实姓名」后，这类行既无法用
    admin 登录，又会因 username 唯一约束导致新建 admin 失败（IntegrityError
    被回滚吞掉），最终表现为 admin 登录后无法发起与管理投票。这里显式把
    历史行的 real_name 迁移为 admin，而不是新插入一行。
    """
    from sqlalchemy.exc import IntegrityError

    admin = User.query.filter_by(real_name=ADMIN_NAME).first()

    if admin is None:
        # 旧库迁移：按 username 找到历史管理员行，就地校正真实姓名
        legacy = User.query.filter_by(username=ADMIN_NAME).first()
        if legacy is not None:
            legacy.real_name = ADMIN_NAME
            legacy.set_password(ADMIN_PASSWORD)
            try:
                db.session.commit()
                admin = legacy
            except IntegrityError:
                db.session.rollback()
                admin = User.query.filter_by(real_name=ADMIN_NAME).first()

    if admin is not None:
        # 校正密码与 username，确保始终是 admin / ADMIN_PASSWORD
        changed = False
        if not admin.check_password(ADMIN_PASSWORD):
            admin.set_password(ADMIN_PASSWORD)
            changed = True
        if admin.username != ADMIN_NAME:
            if User.query.filter_by(username=ADMIN_NAME).first() is None:
                admin.username = ADMIN_NAME
                changed = True
        if changed:
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
        return

    # 全新库：username 字段保留在库中且唯一，这里让它与真实姓名一致
    admin = User(username=ADMIN_NAME, real_name=ADMIN_NAME)
    admin.set_password(ADMIN_PASSWORD)
    db.session.add(admin)
    try:
        db.session.commit()
    except IntegrityError:
        # 另一个 worker 已抢先创建，或存在同名历史行；回滚后重试迁移
        db.session.rollback()
        legacy = User.query.filter_by(username=ADMIN_NAME).first()
        if legacy is not None and legacy.real_name != ADMIN_NAME:
            legacy.real_name = ADMIN_NAME
            legacy.set_password(ADMIN_PASSWORD)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()


def ensure_indexes():
    """为已存在的数据库补建高频查询索引（新库由模型定义自动创建）。

    db.create_all() 不会给已存在的表补索引，这里用 CREATE INDEX IF NOT EXISTS
    兜底，PostgreSQL 与 SQLite 均支持。任何失败都不阻断启动。
    """
    from sqlalchemy import text
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_voters_poll_id ON voters (poll_id)",
        "CREATE INDEX IF NOT EXISTS ix_voters_user_id ON voters (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_ballots_poll_id ON ballots (poll_id)",
        "CREATE INDEX IF NOT EXISTS ix_ballots_voter_id ON ballots (voter_id)",
        "CREATE INDEX IF NOT EXISTS ix_poll_options_poll_id ON poll_options (poll_id)",
        "CREATE INDEX IF NOT EXISTS ix_polls_creator_id ON polls (creator_id)",
        "CREATE INDEX IF NOT EXISTS ix_users_real_name ON users (real_name)",
    ]
    for s in stmts:
        try:
            db.session.execute(text(s))
        except Exception:
            db.session.rollback()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


# 数据库结构升级的记录与错误：部署后访问 /healthz 即可确认升级结果
SCHEMA_UPGRADE_LOG = []
SCHEMA_UPGRADE_ERRORS = []


def _sqlite_rebuild_ballots():
    """SQLite 无法直接删除 UNIQUE 约束，只能「重建表」来替换。

    全程保留原有数据；任一步失败都尽力把它还原回原名，不会丢票。
    （线上的 PostgreSQL 走 ALTER TABLE DROP CONSTRAINT，用不到这里。）
    """
    from sqlalchemy import text

    create_new = (
        "CREATE TABLE ballots_new ("
        " id INTEGER NOT NULL PRIMARY KEY,"
        " poll_id INTEGER NOT NULL,"
        " voter_id INTEGER NOT NULL,"
        " option_id INTEGER NOT NULL,"
        " created_at DATETIME,"
        " CONSTRAINT uq_ballot_poll_voter_option UNIQUE (poll_id, voter_id, option_id),"
        " FOREIGN KEY(poll_id) REFERENCES polls (id),"
        " FOREIGN KEY(voter_id) REFERENCES voters (id),"
        " FOREIGN KEY(option_id) REFERENCES poll_options (id)"
        ")"
    )
    copy_rows = (
        "INSERT INTO ballots_new (id, poll_id, voter_id, option_id, created_at) "
        "SELECT id, poll_id, voter_id, option_id, created_at FROM ballots_legacy"
    )

    try:
        db.session.execute(text("ALTER TABLE ballots RENAME TO ballots_legacy"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        return

    for stmt in (create_new, copy_rows, "DROP TABLE ballots_legacy",
                 "ALTER TABLE ballots_new RENAME TO ballots"):
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()
            # 尽力还原：保证 ballots 这个名字上永远有一张带数据的表
            try:
                db.session.execute(text("DROP TABLE IF EXISTS ballots_new"))
                db.session.execute(text("ALTER TABLE ballots_legacy RENAME TO ballots"))
                db.session.commit()
            except Exception:
                db.session.rollback()
            return


def ensure_schema_upgrades():
    """把「历史数据库」的表结构升级到当前版本（幂等，可反复执行，绝不清数据）。

    为什么必须有这个函数：db.create_all() 只会创建整张不存在的表，
    它既不会给已存在的表补新列，也不会改动已存在的唯一约束。
    线上库是上一版本建好的，如果只靠 create_all：
      1) polls.votes_per_voter、voters.abstained 两列永远不会出现，
         SQLAlchemy 一查询就抛 UndefinedColumn，全站直接 500；
      2) ballots 上旧的唯一约束 (poll_id, voter_id) 会把「同一个人投第二票」
         直接变成 IntegrityError，多票功能当场失效。
    所以这里用原生 SQL 显式补齐，PostgreSQL 与 SQLite 都兼容。

    所有语句独立 try/except，失败只记录不阻断启动；真正的结果由
    verify_schema() 复查并暴露在 /healthz 上，方便部署后一眼确认。
    """
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
    except Exception as e:
        SCHEMA_UPGRADE_ERRORS.append("读取数据库结构失败：%s" % e)
        return

    dialect = db.engine.dialect.name
    bool_default = "FALSE" if dialect == "postgresql" else "0"

    def columns_of(table):
        try:
            return {c["name"] for c in inspector.get_columns(table)}
        except Exception:
            return set()

    def run(sql, strict=True, label=None):
        try:
            db.session.execute(text(sql))
            db.session.commit()
            if label:
                SCHEMA_UPGRADE_LOG.append(label)
            return True
        except Exception as e:
            db.session.rollback()
            if strict:
                SCHEMA_UPGRADE_ERRORS.append("%s 失败：%s" % (label or sql[:60], str(e)[:160]))
            return False

    # 1) polls.votes_per_voter —— 每人票数，旧数据补 1（= 原来的单选行为）
    poll_cols = columns_of("polls")
    if poll_cols and "votes_per_voter" not in poll_cols:
        if run("ALTER TABLE polls ADD COLUMN votes_per_voter INTEGER DEFAULT 1",
               label="为 polls 增加 votes_per_voter 列"):
            run("UPDATE polls SET votes_per_voter = 1 WHERE votes_per_voter IS NULL",
                label="把已有投票的每人票数补为 1")

    # 2) voters.abstained —— 是否弃票，旧数据补 False
    voter_cols = columns_of("voters")
    if voter_cols and "abstained" not in voter_cols:
        if run("ALTER TABLE voters ADD COLUMN abstained BOOLEAN DEFAULT %s" % bool_default,
               label="为 voters 增加 abstained 列"):
            run("UPDATE voters SET abstained = %s WHERE abstained IS NULL" % bool_default,
                label="把已有投票者的弃票标记补为否")

    # 3) ballots 唯一约束：(poll_id, voter_id) -> (poll_id, voter_id, option_id)
    if "ballots" in tables:
        uniques = []
        try:
            uniques = inspector.get_unique_constraints("ballots") or []
        except Exception:
            uniques = []

        has_legacy = any(
            set(u.get("column_names") or []) == {"poll_id", "voter_id"} for u in uniques
        )
        has_current = any(
            set(u.get("column_names") or []) == {"poll_id", "voter_id", "option_id"}
            for u in uniques
        )

        if dialect != "postgresql":
            # SQLite 的 UNIQUE 约束在 PRAGMA 里是匿名 autoindex，名字可能取不到；
            # 直接读建表 SQL 判断最可靠（本地开发库走这条路径）。
            try:
                ddl = db.session.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='ballots'"
                )).scalar() or ""
                has_legacy = re.search(r"\buq_ballot_poll_voter\b", ddl) is not None
                has_current = "uq_ballot_poll_voter_option" in ddl
            except Exception:
                db.session.rollback()

        if has_legacy:
            if dialect == "postgresql":
                for u in uniques:
                    if set(u.get("column_names") or []) == {"poll_id", "voter_id"} and u.get("name"):
                        name = str(u["name"]).replace('"', '""')
                        run('ALTER TABLE ballots DROP CONSTRAINT IF EXISTS "%s"' % name,
                            label="删除 ballots 旧的「一人一票」唯一约束")
                        # 某些库上它是独立唯一索引而非约束，兜底再删一次索引（失败无妨）
                        run('DROP INDEX IF EXISTS "%s"' % name, strict=False)
            else:
                SCHEMA_UPGRADE_LOG.append("重建 ballots 表以放开一人多票")
                _sqlite_rebuild_ballots()

        if not has_current and dialect == "postgresql":
            run("ALTER TABLE ballots ADD CONSTRAINT uq_ballot_poll_voter_option "
                "UNIQUE (poll_id, voter_id, option_id)",
                label="为 ballots 建立「同一人不能重复投同一选项」唯一约束")


def verify_schema():
    """迁移后自检：关键字段必须真的存在，否则把问题暴露到 /healthz。

    这样部署完访问一次 /healthz 就能确认数据库升级是否成功，
    不用等用户报错才发现。
    """
    from sqlalchemy import inspect

    missing = []
    try:
        inspector = inspect(db.engine)
        if "votes_per_voter" not in {c["name"] for c in inspector.get_columns("polls")}:
            missing.append("polls.votes_per_voter")
        if "abstained" not in {c["name"] for c in inspector.get_columns("voters")}:
            missing.append("voters.abstained")
    except Exception as e:
        missing.append("结构自检异常：%s" % e)

    if missing:
        SCHEMA_UPGRADE_ERRORS.append("升级后仍缺少字段：%s" % "、".join(missing))
    else:
        SCHEMA_UPGRADE_LOG.append("结构自检通过")


with app.app_context():
    ensure_schema_upgrades()
    verify_schema()
    ensure_admin_account()
    ensure_indexes()


def is_same_person(a: str, b: str) -> bool:
    """姓名比对：忽略所有空白字符与英文大小写。

    用于「不能投给自己」的判定。选项名与登录者真实姓名一致（即使中间多打了
    空格、或英文名大小写不同）都视为同一个人，确保自己绝对投不到自己。
    """
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"\s+", "", s).casefold()
    return norm(a) == norm(b)


def parse_names_from_text(text: str):
    """从文本中解析姓名列表：支持换行、逗号、中文逗号、顿号、分号、空格分隔。"""
    if not text:
        return []
    parts = re.split(r"[\n\r,，、;；\t]+", text)
    names, seen = [], set()
    for p in parts:
        name = p.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def parse_names_from_excel(file_storage):
    """从 Excel/CSV 读取名单：取第一列所有非空单元格（跳过明显表头）。"""
    filename = (file_storage.filename or "").lower()
    names = []
    if filename.endswith(".csv"):
        raw = file_storage.read().decode("utf-8-sig", errors="ignore")
        for line in raw.splitlines():
            cell = line.split(",")[0].strip().strip('"')
            if cell:
                names.append(cell)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_storage.read()), data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            cell = row[0]
            if cell is None:
                continue
            names.append(str(cell).strip())
    header_words = {"姓名", "名字", "name", "投票者", "名单", "序号", ""}
    result, seen = [], set()
    for n in names:
        if not n or n.lower() in header_words or n in header_words:
            continue
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


def link_voters_to_users(poll):
    """把名单里的姓名尝试匹配到已注册用户（按 real_name）。

    性能优化：
    - 先筛出尚未关联的 voter，若没有则直接返回，避免无谓写事务；
    - 用一次 IN 查询批量取回相关用户，消除逐个 voter 的 N+1 查询；
    - 只有真正产生了新关联时才 commit。
    """
    pending = [v for v in poll.voters if not v.user_id]
    if not pending:
        return

    names = {v.name for v in pending}
    users = User.query.filter(User.real_name.in_(names)).all()
    name_to_uid = {u.real_name: u.id for u in users}

    changed = False
    for voter in pending:
        uid = name_to_uid.get(voter.name)
        if uid:
            voter.user_id = uid
            changed = True

    if changed:
        db.session.commit()


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    user = current_user()
    if not user:
        return render_template("index.html", polls=None)
    created = Poll.query.filter_by(creator_id=user.id).order_by(Poll.created_at.desc()).all()
    # 我被列入名单的投票
    my_voter_rows = Voter.query.filter_by(user_id=user.id).all()
    invited = [v.poll for v in my_voter_rows if v.poll and v.poll.creator_id != user.id]
    invited = list({p.id: p for p in invited}.values())
    return render_template("index.html", polls=created, invited=invited)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        real_name = (request.form.get("real_name") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not real_name or not password:
            flash("真实姓名、密码均不能为空", "error")
            return render_template("register.html")
        if len(real_name) > MAX_NAME_LEN:
            flash("真实姓名过长（最多 %d 个字符，当前 %d 个）"
                  % (MAX_NAME_LEN, len(real_name)), "error")
            return render_template("register.html")
        if len(password) > MAX_PASSWORD_LEN:
            flash("密码过长（最多 %d 位，当前 %d 位）" % (MAX_PASSWORD_LEN, len(password)), "error")
            return render_template("register.html")
        if password != password2:
            flash("两次输入的密码不一致", "error")
            return render_template("register.html")
        pwd_error = validate_password_charset(password)
        if pwd_error:
            flash(pwd_error, "error")
            return render_template("register.html")
        if real_name == ADMIN_NAME:
            flash("该姓名为系统保留，请更换", "error")
            return render_template("register.html")
        if User.query.filter_by(real_name=real_name).first():
            flash("该真实姓名已被注册，姓名只能唯一", "error")
            return render_template("register.html")

        # 用真实姓名作为账号；底层 username 字段与真实姓名保持一致
        user = User(username=real_name, real_name=real_name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        flash("注册成功，已自动登录", "ok")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        real_name = (request.form.get("real_name") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(real_name=real_name).first()
        if not user or not user.check_password(password):
            flash("姓名或密码错误", "error")
            return render_template("login.html")
        # 透明迁移：历史哈希账号登录成功后，把密码转存为明文，
        # 之后该账号登录即为零开销明文比对。不影响现有数据、无需清库。
        try:
            if not user.is_plain:
                user.set_password(password)
                db.session.commit()
        except Exception:
            db.session.rollback()
        session["user_id"] = user.id
        flash("登录成功", "ok")
        nxt = request.args.get("next")
        return redirect(nxt or url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 发起投票
# ---------------------------------------------------------------------------
@app.route("/poll/create", methods=["GET", "POST"])
@admin_required
def create_poll():
    user = current_user()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        allow_self_vote = request.form.get("allow_self_vote") == "yes"
        allow_abstain = request.form.get("allow_abstain") == "on"
        try:
            votes_per_voter = int((request.form.get("votes_per_voter") or "1").strip() or "1")
        except (TypeError, ValueError):
            flash("每人票数必须是整数", "error")
            return render_template("create_poll.html")

        option_names = [o.strip() for o in request.form.getlist("options") if o.strip()]
        # 去重保序
        seen, options = set(), []
        for o in option_names:
            if o not in seen:
                seen.add(o)
                options.append(o)

        if not title:
            flash("请填写投票标题", "error")
            return render_template("create_poll.html")
        if len(title) > MAX_TITLE_LEN:
            flash("投票标题过长（最多 %d 个字符，当前 %d 个）"
                  % (MAX_TITLE_LEN, len(title)), "error")
            return render_template("create_poll.html")
        if len(options) < 2:
            flash("至少需要 2 个有效选项", "error")
            return render_template("create_poll.html")
        too_long_options = [o for o in options if len(o) > MAX_OPTION_LEN]
        if too_long_options:
            flash("选项名过长（最多 %d 个字符）：%s"
                  % (MAX_OPTION_LEN, "、".join(o[:12] + "…" for o in too_long_options[:3])),
                  "error")
            return render_template("create_poll.html")
        if votes_per_voter < 1:
            flash("每人票数至少为 1", "error")
            return render_template("create_poll.html")
        if votes_per_voter > 100:
            flash("每人票数过大，上限为 100", "error")
            return render_template("create_poll.html")

        # 关键校验：每人必须投满 N 票、且不能重复投给同一人 → 可选对象必须够多。
        # 不允许投自己时，每个投票者都会少掉「与自己同名的那一项」，
        # 所以要留出 +1 的余量，否则会出现「永远投不满、只能弃票」的死局。
        capacity = len(options) if allow_self_vote else len(options) - 1
        if votes_per_voter > capacity:
            flash(
                "每人 %d 票，但可投对象最多只有 %d 个（当前 %d 个选项%s）。"
                "请增加选项数量，或调低每人票数。"
                % (votes_per_voter, max(capacity, 0), len(options),
                   "，且当前设置为不允许投自己" if not allow_self_vote else ""),
                "error",
            )
            return render_template("create_poll.html")

        poll = Poll(
            title=title,
            description=description,
            creator_id=user.id,
            allow_self_vote=allow_self_vote,
            allow_abstain=allow_abstain,
            votes_per_voter=votes_per_voter,
        )
        db.session.add(poll)
        db.session.flush()

        for i, name in enumerate(options):
            db.session.add(PollOption(poll_id=poll.id, name=name, position=i))
        # 注意：不再把「弃票」做成一个选项。
        # 弃票 = 该投票人主动一票不投，用 voter.abstained 记录，
        # 这样它不会被统计成投给某个人的票，导出时也能明确标注。

        # 名单：文本 + 上传文件
        names = parse_names_from_text(request.form.get("voter_names", ""))
        upload = request.files.get("voter_file")
        if upload and upload.filename:
            fn = upload.filename.lower()
            if fn.endswith((".xlsx", ".xls", ".csv")):
                try:
                    names += parse_names_from_excel(upload)
                except Exception as e:
                    flash(f"名单文件解析失败：{e}", "warn")
            else:
                flash("图片名单需手动录入姓名；已忽略图片文件（无法自动识别文字）", "warn")

        # 发起人不能在名单中
        names, dropped = filter_long_names(names)
        if dropped:
            flash("已忽略 %d 个过长的姓名（上限 %d 字）：%s"
                  % (len(dropped), MAX_NAME_LEN,
                     "、".join(d[:12] + "…" for d in dropped[:3])), "warn")

        creator_name = user.real_name
        final_names, seen = [], set()
        for n in names:
            if n and n not in seen:
                seen.add(n)
                if n == creator_name:
                    continue  # 发起人不可参与投票
                final_names.append(n)

        for n in final_names:
            db.session.add(Voter(poll_id=poll.id, name=n))
        db.session.commit()

        link_voters_to_users(poll)
        flash("投票创建成功", "ok")
        return redirect(url_for("manage_poll", poll_id=poll.id))

    return render_template("create_poll.html")


# ---------------------------------------------------------------------------
# 发起人管理页
# ---------------------------------------------------------------------------
@app.route("/poll/<int:poll_id>/manage")
@admin_required
def manage_poll(poll_id):
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll:
        flash("投票不存在", "error")
        return redirect(url_for("index"))
    if poll.creator_id != user.id:
        flash("只有发起人可以管理该投票", "error")
        return redirect(url_for("index"))
    link_voters_to_users(poll)
    return render_template("manage_poll.html", poll=poll)


@app.route("/poll/<int:poll_id>/status")
@admin_required
def poll_status(poll_id):
    """发起人页面轮询：返回名单投票状态、票数明细，供动态刷新。"""
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll or poll.creator_id != user.id:
        return jsonify({"error": "forbidden"}), 403

    link_voters_to_users(poll)

    voters = sorted(poll.voters, key=lambda v: v.name)

    abstain_opt_ids = {o.id for o in poll.options if o.is_abstain}
    ballots_by_voter = {}
    for b in poll.ballots:
        ballots_by_voter.setdefault(b.voter_id, []).append(b)

    def _is_abstainer(v):
        """明确弃票，或（历史数据）把票投给了「弃票」选项。"""
        if v.abstained:
            return True
        bs = ballots_by_voter.get(v.id) or []
        return bool(bs) and all(b.option_id in abstain_opt_ids for b in bs)

    voted = [{"name": v.name,
              "abstained": _is_abstainer(v),
              "voted_at": v.voted_at.strftime("%Y-%m-%d %H:%M") if v.voted_at else None}
             for v in voters if v.has_voted]
    not_voted = [{"name": v.name, "registered": v.user_id is not None}
                 for v in voters if not v.has_voted]

    # 票数统计
    counts = {opt.id: 0 for opt in poll.options}
    detail = {opt.id: [] for opt in poll.options}
    for b in poll.ballots:
        counts[b.option_id] = counts.get(b.option_id, 0) + 1
        detail[b.option_id].append(b.voter.name if b.voter else "?")

    options = [{
        "id": opt.id,
        "name": opt.name,
        "is_abstain": opt.is_abstain,
        "count": counts.get(opt.id, 0),
        "voters": sorted(detail.get(opt.id, [])),
    } for opt in poll.options]

    abstain_count = sum(1 for v in voters if v.has_voted and _is_abstainer(v))

    return jsonify({
        "is_closed": poll.is_closed,
        "is_published": poll.is_published,
        "show_voter_names": poll.show_voter_names,
        "votes_per_voter": max(1, int(poll.votes_per_voter or 1)),
        "allow_abstain": bool(poll.allow_abstain),
        "allow_self_vote": bool(poll.allow_self_vote),
        "abstain_count": abstain_count,
        "total": len(voters),
        "voted_count": len(voted),
        "not_voted_count": len(not_voted),
        "voted": voted,
        "not_voted": not_voted,
        "options": options,
    })


@app.route("/poll/<int:poll_id>/toggle_names", methods=["POST"])
@admin_required
def toggle_names(poll_id):
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll or poll.creator_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    poll.show_voter_names = not poll.show_voter_names
    db.session.commit()
    return jsonify({"show_voter_names": poll.show_voter_names})


@app.route("/poll/<int:poll_id>/close", methods=["POST"])
@admin_required
def close_poll(poll_id):
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll or poll.creator_id != user.id:
        flash("无权操作", "error")
        return redirect(url_for("index"))
    poll.is_closed = True
    poll.closed_at = datetime.utcnow()
    db.session.commit()
    flash("投票已结束", "ok")
    return redirect(url_for("manage_poll", poll_id=poll.id))


@app.route("/poll/<int:poll_id>/publish", methods=["POST"])
@admin_required
def publish_poll(poll_id):
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll or poll.creator_id != user.id:
        flash("无权操作", "error")
        return redirect(url_for("index"))
    if not poll.is_closed:
        flash("请先结束投票再公布票数", "warn")
        return redirect(url_for("manage_poll", poll_id=poll.id))
    poll.is_published = not poll.is_published
    db.session.commit()
    flash("已更新公布状态", "ok")
    return redirect(url_for("manage_poll", poll_id=poll.id))


@app.route("/poll/<int:poll_id>/add_voter", methods=["POST"])
@admin_required
def add_voter(poll_id):
    """发起人在管理页动态追加名单成员。"""
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll or poll.creator_id != user.id:
        flash("无权操作", "error")
        return redirect(url_for("index"))

    names = parse_names_from_text(request.form.get("voter_names", ""))
    upload = request.files.get("voter_file")
    if upload and upload.filename:
        fn = upload.filename.lower()
        if fn.endswith((".xlsx", ".xls", ".csv")):
            try:
                names += parse_names_from_excel(upload)
            except Exception as e:
                flash(f"名单文件解析失败：{e}", "warn")
        else:
            flash("图片名单无法自动识别，请手动录入", "warn")

    names, dropped = filter_long_names(names)
    if dropped:
        flash("已忽略 %d 个过长的姓名（上限 %d 字）" % (len(dropped), MAX_NAME_LEN), "warn")

    existing = {v.name for v in poll.voters}
    added = 0
    for n in names:
        if n and n not in existing and n != user.real_name:
            db.session.add(Voter(poll_id=poll.id, name=n))
            existing.add(n)
            added += 1
    db.session.commit()
    link_voters_to_users(poll)
    flash(f"新增 {added} 名投票者", "ok")
    return redirect(url_for("manage_poll", poll_id=poll.id))


# ---------------------------------------------------------------------------
# 导出 Excel（仅发起人 / admin）
# ---------------------------------------------------------------------------
def _bj_time(dt):
    """库里存的是 UTC，导出给中国用户看统一转成北京时间（+8）。"""
    if not dt:
        return ""
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def build_poll_workbook(poll):
    """把一场投票整理成 xlsx（返回 BytesIO）。三张表：得票统计 / 投票明细 / 投票矩阵。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    voters = sorted(poll.voters, key=lambda v: v.name)
    abstain_option_ids = {o.id for o in poll.options if o.is_abstain}
    options = [o for o in poll.options if not o.is_abstain]

    ballots_by_voter = {}
    for b in poll.ballots:
        ballots_by_voter.setdefault(b.voter_id, []).append(b)

    def voted_for(v):
        """该投票人实际投给了谁（历史数据的「弃票选项」不算投给某人）。"""
        return [opt_by_id[b.option_id].name
                for b in ballots_by_voter.get(v.id, [])
                if b.option_id in opt_by_id and b.option_id not in abstain_option_ids]

    def is_abstainer(v):
        """本次弃票 = 明确选了弃票，或旧数据里把票投给了「弃票」选项。"""
        if v.abstained:
            return True
        bs = ballots_by_voter.get(v.id) or []
        return bool(bs) and all(b.option_id in abstain_option_ids for b in bs)

    opt_by_id = {o.id: o for o in poll.options}

    counts = {o.id: 0 for o in options}
    for b in poll.ballots:
        if b.option_id in counts:
            counts[b.option_id] += 1
    total_votes = sum(counts.values())

    abstainers = [v for v in voters if v.has_voted and is_abstainer(v)]
    not_voted = [v for v in voters if not v.has_voted]

    wb = Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E6F2EA")
    center = Alignment(horizontal="center")

    # ---------------- 表 1：得票统计（降序，并列票数同名次） ----------------
    ws = wb.active
    ws.title = "得票统计"
    ws.append(["投票标题", poll.title, "", "发起人",
               poll.creator.real_name if poll.creator else ""])
    ws.append(["每人票数", max(1, int(poll.votes_per_voter or 1)), "", "导出时间",
               _bj_time(datetime.utcnow())])
    ws.append(["应投人数", len(voters), "", "实际投出票数", total_votes])
    ws.append(["弃票人数", len(abstainers), "", "未投票人数", len(not_voted)])
    ws.append([""])
    ws.append(["名次", "姓名", "得票数", "得票率"])
    head_row = ws.max_row
    for c in ws[head_row]:
        c.font = bold
        c.fill = head_fill
        c.alignment = center

    ranked = sorted(options, key=lambda o: (-counts[o.id], o.position))
    prev_count, prev_rank = None, None
    for i, o in enumerate(ranked):
        c = counts[o.id]
        if c == prev_count:
            rank = prev_rank          # 并列票数 → 同名次
        else:
            rank = i + 1
            prev_count, prev_rank = c, rank
        ws.append([rank, o.name, c,
                   ("%.1f%%" % (c * 100.0 / total_votes)) if total_votes else "0.0%"])
    # 弃票单独一行，明确标注，不混进名次
    ws.append(["—", "弃票（一票未投）", len(abstainers), "—"])
    ws.freeze_panes = "A%d" % (head_row + 1)

    # ---------------- 表 2：投票明细（谁 / 什么时候投的 / 投给了谁） ----------------
    ws2 = wb.create_sheet("投票明细")
    ws2.append(["序号", "姓名", "状态", "投票时间", "投给了谁"])
    for c in ws2[1]:
        c.font = bold
        c.fill = head_fill
        c.alignment = center

    def sort_key(v):
        if v.has_voted and not is_abstainer(v):
            return (0, v.voted_at or datetime.min, v.name)
        if v.has_voted:
            return (1, v.voted_at or datetime.min, v.name)
        return (2, datetime.min, v.name)

    for idx, v in enumerate(sorted(voters, key=sort_key), start=1):
        if not v.has_voted:
            status, when, whom = "未投票", "", ""
        elif is_abstainer(v):
            status, when, whom = "弃票", _bj_time(v.voted_at), "弃票（未投给任何人）"
        else:
            names = voted_for(v)
            status = "已投票"
            when = _bj_time(v.voted_at)
            whom = "、".join(names) if names else ""
        ws2.append([idx, v.name, status, when, whom])
    ws2.freeze_panes = "A2"

    # ---------------- 表 3：投票矩阵（行=投票人，列=候选人） ----------------
    ws3 = wb.create_sheet("投票矩阵")
    ws3.append(["姓名", "投票时间", "状态"] + [o.name for o in options])
    for c in ws3[1]:
        c.font = bold
        c.fill = head_fill
        c.alignment = center
    for v in sorted(voters, key=sort_key):
        if not v.has_voted:
            row = [v.name, "", "未投票"]
        elif is_abstainer(v):
            row = [v.name, _bj_time(v.voted_at), "弃票"]
        else:
            row = [v.name, _bj_time(v.voted_at), "已投票"]
        got = set(voted_for(v))
        row += ["√" if o.name in got else "" for o in options]
        ws3.append(row)
    ws3.freeze_panes = "D2"

    # 列宽自适应（中文按 2 个字符宽估算，粗糙但够用）
    for sheet in wb.worksheets:
        for col in sheet.columns:
            width = 8
            for cell in col:
                if cell.value is None:
                    continue
                text = str(cell.value)
                w = sum(2 if ord(ch) > 127 else 1 for ch in text)
                width = max(width, min(w + 4, 46))
            sheet.column_dimensions[col[0].column_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/poll/<int:poll_id>/export")
@admin_required
def export_poll(poll_id):
    """发起人一键导出本场投票的 Excel。投票进行中也能导（实时快照）。"""
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll:
        flash("投票不存在", "error")
        return redirect(url_for("index"))
    if poll.creator_id != user.id:
        flash("只有发起人可以导出该投票的结果", "error")
        return redirect(url_for("index"))

    try:
        buf = build_poll_workbook(poll)
    except Exception as e:
        flash("导出失败：%s" % e, "error")
        return redirect(url_for("manage_poll", poll_id=poll.id))

    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", poll.title or "poll")[:40] or "poll"
    stamp = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        as_attachment=True,
        download_name="投票结果_%s_%s.xlsx" % (safe_title, stamp),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# 投票者投票
# ---------------------------------------------------------------------------
@app.route("/poll/<int:poll_id>/vote", methods=["GET", "POST"])
@login_required
def vote(poll_id):
    user = current_user()
    poll = db.session.get(Poll, poll_id)
    if not poll:
        flash("投票不存在", "error")
        return redirect(url_for("index"))

    if poll.creator_id == user.id:
        flash("发起人不能参与自己发起的投票", "warn")
        return redirect(url_for("manage_poll", poll_id=poll.id))

    # 性能：投票路径不再对整份名单做关联遍历，只精确定位“我自己”的名单记录。
    voter = Voter.query.filter_by(poll_id=poll.id, user_id=user.id).first()
    if not voter:
        # 兜底：按真实姓名匹配，并只回填自己这一条的关联
        voter = Voter.query.filter_by(poll_id=poll.id, name=user.real_name).first()
        if voter and not voter.user_id:
            voter.user_id = user.id
            db.session.commit()
    if not voter:
        flash("你不在本次投票的名单中，无法投票", "error")
        return redirect(url_for("index"))

    quota = max(1, int(poll.votes_per_voter or 1))
    # 是否已投票只看 has_voted：弃票时没有任何 Ballot 记录，
    # 若继续用「有没有 ballot」判断，弃过票的人会被当成没投过而重复提交。
    has_voted = bool(voter.has_voted)

    my_ballots = Ballot.query.filter_by(poll_id=poll.id, voter_id=voter.id).all()
    opt_by_id = {o.id: o for o in poll.options}
    my_option_ids = {b.option_id for b in my_ballots}
    my_choices = [o.name for o in poll.options
                  if o.id in my_option_ids and not o.is_abstain]
    # 兼容历史数据：旧版本的「弃票」是一个真实选项，投给它的人视同弃票
    legacy_abstain_only = bool(my_ballots) and all(
        opt_by_id[b.option_id].is_abstain
        for b in my_ballots if b.option_id in opt_by_id
    )
    my_abstained = bool(voter.abstained) or legacy_abstain_only

    if request.method == "POST":
        if poll.is_closed:
            flash("投票已结束，无法再投", "warn")
            return redirect(url_for("vote", poll_id=poll.id))
        if has_voted:
            flash("你已经投过票了", "warn")
            return redirect(url_for("vote", poll_id=poll.id))

        # ---------------- 弃票：一票都不投 ----------------
        if request.form.get("abstain") == "1":
            if not poll.allow_abstain:
                flash("本场投票不允许弃票", "error")
                return redirect(url_for("vote", poll_id=poll.id))
            # 弃票与投票互斥：选了弃票就不允许再带走任何一张票
            if request.form.getlist("option_ids"):
                flash("你已选择弃票，无法再投出任何一票；若要投票请先取消弃票", "error")
                return redirect(url_for("vote", poll_id=poll.id))
            voter.abstained = True
            voter.has_voted = True
            voter.voted_at = datetime.utcnow()
            db.session.commit()
            flash("已提交弃票：本次你没有投出任何一票", "ok")
            return redirect(url_for("vote", poll_id=poll.id))

        # ---------------- 正常投票：必须投满 N 票 ----------------
        raw_ids = request.form.getlist("option_ids") or request.form.getlist("option_id")
        ids, seen_ids = [], set()
        for x in raw_ids:
            try:
                oid = int(x)
            except (TypeError, ValueError):
                continue
            if oid not in seen_ids:
                seen_ids.add(oid)
                ids.append(oid)

        if len(ids) != quota:
            flash(
                "本次投票每人必须投满 %d 票，且必须投给不同的人（你当前选了 %d 票）。"
                % (quota, len(ids)),
                "error",
            )
            return redirect(url_for("vote", poll_id=poll.id))

        opts = PollOption.query.filter(
            PollOption.poll_id == poll.id, PollOption.id.in_(ids)
        ).all()
        if len(opts) != len(ids):
            flash("选项无效，请重新选择", "error")
            return redirect(url_for("vote", poll_id=poll.id))

        for opt in opts:
            if opt.is_abstain:
                flash("「弃票」不能与其他选项一起提交，请单独选择弃票", "error")
                return redirect(url_for("vote", poll_id=poll.id))
            # 不能投给自己：选项名与自己的真实姓名一致即视为本人
            if (not poll.allow_self_vote) and is_same_person(opt.name, user.real_name):
                flash("本次投票不允许投给自己（选项「%s」与你的姓名一致）" % opt.name, "error")
                return redirect(url_for("vote", poll_id=poll.id))

        now = datetime.utcnow()
        for opt in opts:
            db.session.add(Ballot(poll_id=poll.id, voter_id=voter.id,
                                  option_id=opt.id, created_at=now))
        voter.abstained = False
        voter.has_voted = True
        voter.voted_at = now
        try:
            db.session.commit()
        except IntegrityError:
            # 极端并发（同一人连点两次 / 多设备同时提交）：唯一约束挡下重复选票。
            # 整个事务已回滚，不会出现「投了一半」的残缺记录。
            db.session.rollback()
            fresh = db.session.get(Voter, voter.id)
            if fresh is not None and fresh.has_voted:
                flash("你已经投过票了", "warn")
            else:
                flash("提交发生冲突（可能重复点击），请刷新页面确认后再试", "warn")
            return redirect(url_for("vote", poll_id=poll.id))
        flash("投票成功，共投出 %d 票" % len(opts), "ok")
        return redirect(url_for("vote", poll_id=poll.id))

    # 展示：投票者只能看到自己的选择，看不到他人结果与票数
    # 可投选项：排除「弃票」选项，以及（不允许投自己时）与自己同名的选项 ——
    # 从列表里直接拿掉，自己那一项连出现的机会都没有。
    options = []
    for opt in poll.options:
        if opt.is_abstain:
            continue
        if (not poll.allow_self_vote) and is_same_person(opt.name, user.real_name):
            continue
        options.append(opt)

    # 发起人公布票数后，投票者可看到各选项票数（仍匿名，不显示谁投的）
    results = None
    total_votes = 0
    if poll.is_published:
        counts = {opt.id: 0 for opt in poll.options}
        for b in poll.ballots:
            counts[b.option_id] = counts.get(b.option_id, 0) + 1
        total_votes = sum(counts.values())
        results = [{
            "name": opt.name,
            "is_abstain": opt.is_abstain,
            "count": counts.get(opt.id, 0),
            "percent": round(counts.get(opt.id, 0) * 100 / total_votes, 1) if total_votes else 0,
        } for opt in poll.options]

    return render_template("vote.html", poll=poll, options=options,
                           quota=quota, has_voted=has_voted,
                           my_choices=my_choices, my_abstained=my_abstained,
                           results=results, total_votes=total_votes)


@app.errorhandler(413)
def too_large(e):
    flash("上传文件过大（上限 8MB）", "error")
    return redirect(url_for("index"))


@app.errorhandler(500)
def internal_error(e):
    """兜底：任何未预料的异常都给一个友好页面，不把堆栈暴露给用户。

    先回滚会话，避免坏事务污染同一个 worker 后续的请求。
    """
    try:
        db.session.rollback()
    except Exception:
        pass
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>服务器出错</title></head>"
        "<body style=\"font-family:'Microsoft YaHei',sans-serif;padding:48px;color:#1f2a24;\">"
        "<h2 style=\"color:#0a5730;margin:0 0 12px;\">服务器出了一点问题</h2>"
        "<p style=\"color:#6b7c72;\">请返回上一页重试。若持续出现，请把操作步骤告诉发起人。</p>"
        "<p><a href=\"/\" style=\"color:#0e6a3b;\">返回首页</a></p>"
        "</body></html>"
    ), 500


@app.route("/healthz")
def healthz():
    """健康检查 + 数据库结构升级结果（部署后访问这个地址即可自检）。

    status = ok       → 一切正常
    status = degraded → 数据库结构升级有问题，errors 里写明原因
    """
    if SCHEMA_UPGRADE_ERRORS:
        return jsonify({"status": "degraded", "errors": SCHEMA_UPGRADE_ERRORS}), 200
    return jsonify({"status": "ok", "schema_upgrades": SCHEMA_UPGRADE_LOG}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
