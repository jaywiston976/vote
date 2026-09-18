import io
import os
import re
from datetime import datetime
from functools import wraps

from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, session, url_for,
)

from config import Config
from models import Ballot, Poll, PollOption, User, Voter, db

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

with app.app_context():
    # 受环境变量开关控制的一次性数据库重置：
    # 在 Render 设置 RESET_DB=1 并触发一次部署即可清空所有数据并重建表结构；
    # 清空后请把该环境变量删除或改为 0，避免此后每次重启都清库。
    if os.environ.get("RESET_DB", "0") == "1":
        db.drop_all()
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


# 唯一允许发起 / 管理投票的账号
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "123"


def is_admin(user) -> bool:
    """仅 admin 这一特定账号可以发起 / 管理投票。"""
    return bool(user) and user.username == ADMIN_USERNAME


def ensure_admin_account():
    """确保存在唯一的管理员账号 admin / 123。

    抗并发：gunicorn 多 worker 会同时执行本函数，若都判定 admin 不存在
    并各自 INSERT，会触发唯一约束冲突。这里捕获 IntegrityError 回滚重查，
    保证任意 worker、任意次数执行都安全且结果一致。
    每次启动都把 admin 密码校正为 123，避免旧库里的旧密码残留。
    """
    from sqlalchemy.exc import IntegrityError

    admin = User.query.filter_by(username=ADMIN_USERNAME).first()
    if admin:
        # 校正密码，确保始终是 123
        if not admin.check_password(ADMIN_PASSWORD):
            admin.set_password(ADMIN_PASSWORD)
            db.session.commit()
        return

    real_name = "管理员"
    if User.query.filter_by(real_name=real_name).first():
        real_name = "系统管理员"
    admin = User(username=ADMIN_USERNAME, real_name=real_name)
    admin.set_password(ADMIN_PASSWORD)
    db.session.add(admin)
    try:
        db.session.commit()
    except IntegrityError:
        # 另一个 worker 已抢先创建，回滚即可
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


with app.app_context():
    ensure_admin_account()
    ensure_indexes()


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
        username = (request.form.get("username") or "").strip()
        real_name = (request.form.get("real_name") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not username or not real_name or not password:
            flash("用户名、真实姓名、密码均不能为空", "error")
            return render_template("register.html")
        if password != password2:
            flash("两次输入的密码不一致", "error")
            return render_template("register.html")
        pwd_error = validate_password_charset(password)
        if pwd_error:
            flash(pwd_error, "error")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("该用户名已被使用，请更换", "error")
            return render_template("register.html")
        if User.query.filter_by(real_name=real_name).first():
            flash("该真实姓名已被注册，实名只能唯一", "error")
            return render_template("register.html")

        user = User(username=username, real_name=real_name)
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
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("用户名或密码错误", "error")
            return render_template("login.html")
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
        if len(options) < 2:
            flash("至少需要 2 个有效选项", "error")
            return render_template("create_poll.html")

        poll = Poll(
            title=title,
            description=description,
            creator_id=user.id,
            allow_self_vote=allow_self_vote,
            allow_abstain=allow_abstain,
        )
        db.session.add(poll)
        db.session.flush()

        for i, name in enumerate(options):
            db.session.add(PollOption(poll_id=poll.id, name=name, position=i))
        if allow_abstain:
            db.session.add(PollOption(
                poll_id=poll.id, name="弃票", position=len(options), is_abstain=True
            ))

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
    voted = [{"name": v.name, "voted_at": v.voted_at.strftime("%Y-%m-%d %H:%M") if v.voted_at else None}
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

    return jsonify({
        "is_closed": poll.is_closed,
        "is_published": poll.is_published,
        "show_voter_names": poll.show_voter_names,
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

    link_voters_to_users(poll)
    voter = Voter.query.filter_by(poll_id=poll.id, user_id=user.id).first()
    if not voter:
        # 兜底：按真实姓名匹配
        voter = Voter.query.filter_by(poll_id=poll.id, name=user.real_name).first()
        if voter and not voter.user_id:
            voter.user_id = user.id
            db.session.commit()
    if not voter:
        flash("你不在本次投票的名单中，无法投票", "error")
        return redirect(url_for("index"))

    my_ballot = Ballot.query.filter_by(poll_id=poll.id, voter_id=voter.id).first()

    if request.method == "POST":
        if poll.is_closed:
            flash("投票已结束，无法再投", "warn")
            return redirect(url_for("vote", poll_id=poll.id))
        if my_ballot:
            flash("你已经投过票了", "warn")
            return redirect(url_for("vote", poll_id=poll.id))

        option_id = request.form.get("option_id")
        opt = db.session.get(PollOption, int(option_id)) if option_id else None
        if not opt or opt.poll_id != poll.id:
            flash("请选择一个有效选项", "error")
            return redirect(url_for("vote", poll_id=poll.id))

        # 是否允许投自己
        if not poll.allow_self_vote and not opt.is_abstain and opt.name == user.real_name:
            flash("本次投票不允许投给自己", "error")
            return redirect(url_for("vote", poll_id=poll.id))

        ballot = Ballot(poll_id=poll.id, voter_id=voter.id, option_id=opt.id)
        db.session.add(ballot)
        voter.has_voted = True
        voter.voted_at = datetime.utcnow()
        db.session.commit()
        flash("投票成功", "ok")
        return redirect(url_for("vote", poll_id=poll.id))

    # 展示：投票者只能看到自己的选择，看不到他人结果与票数
    my_choice = None
    if my_ballot:
        opt = db.session.get(PollOption, my_ballot.option_id)
        my_choice = opt.name if opt else None

    # 可投选项：若不允许投自己，隐藏与自己同名的选项
    options = []
    for opt in poll.options:
        if (not poll.allow_self_vote) and (not opt.is_abstain) and opt.name == user.real_name:
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
                           my_choice=my_choice, has_voted=my_ballot is not None,
                           results=results, total_votes=total_votes)


@app.errorhandler(413)
def too_large(e):
    flash("上传文件过大（上限 8MB）", "error")
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
