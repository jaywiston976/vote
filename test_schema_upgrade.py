# -*- coding: utf-8 -*-
"""升级验证脚本（临时，验证完可删）。

做两件事：
  A. 按「上一版本」的真实表结构造一个旧 SQLite 库（含旧唯一约束 + 旧数据），
     再用新代码启动，验证能否原地升级、旧数据是否完好；
  B. 端到端跑新功能：自定义票数 / 必须投满 / 不能重复投同一人 / 弃票 /
     不能投自己 / 导出 Excel（并列名次、时间、投给了谁、弃票标注）。
"""
import io
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="vote_upgrade_")
DB_PATH = os.path.join(TMP, "legacy.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH.replace("\\", "/")
os.environ.setdefault("SECRET_KEY", "test-secret")
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(label, ok, extra=""):
    (PASS if ok else FAIL).append(label)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------------------
# A. 造旧库
# ---------------------------------------------------------------------------
def build_legacy_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE users (
            id INTEGER NOT NULL PRIMARY KEY,
            username VARCHAR(64) NOT NULL,
            real_name VARCHAR(64) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at DATETIME,
            UNIQUE (username)
        );
        CREATE INDEX ix_users_real_name ON users (real_name);
        CREATE TABLE polls (
            id INTEGER NOT NULL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            description TEXT,
            creator_id INTEGER NOT NULL,
            allow_self_vote BOOLEAN,
            allow_abstain BOOLEAN,
            is_closed BOOLEAN,
            is_published BOOLEAN,
            show_voter_names BOOLEAN,
            created_at DATETIME,
            closed_at DATETIME,
            FOREIGN KEY(creator_id) REFERENCES users (id)
        );
        CREATE TABLE poll_options (
            id INTEGER NOT NULL PRIMARY KEY,
            poll_id INTEGER NOT NULL,
            name VARCHAR(200) NOT NULL,
            position INTEGER,
            is_abstain BOOLEAN,
            FOREIGN KEY(poll_id) REFERENCES polls (id)
        );
        CREATE TABLE voters (
            id INTEGER NOT NULL PRIMARY KEY,
            poll_id INTEGER NOT NULL,
            name VARCHAR(64) NOT NULL,
            user_id INTEGER,
            has_voted BOOLEAN,
            voted_at DATETIME,
            CONSTRAINT uq_voter_poll_name UNIQUE (poll_id, name),
            FOREIGN KEY(poll_id) REFERENCES polls (id),
            FOREIGN KEY(user_id) REFERENCES users (id)
        );
        CREATE TABLE ballots (
            id INTEGER NOT NULL PRIMARY KEY,
            poll_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            created_at DATETIME,
            CONSTRAINT uq_ballot_poll_voter UNIQUE (poll_id, voter_id),
            FOREIGN KEY(poll_id) REFERENCES polls (id),
            FOREIGN KEY(voter_id) REFERENCES voters (id),
            FOREIGN KEY(option_id) REFERENCES poll_options (id)
        );
        """
    )
    c.execute("INSERT INTO users VALUES (1,'admin','admin','plain:muyu123','2026-09-19 00:00:00')")
    for i, n in enumerate(["张三", "李四"], start=2):
        c.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                  (i, n, n, "plain:123", "2026-09-19 00:00:00"))
    c.execute("INSERT INTO polls VALUES (1,'旧版单选投票','历史数据',1,0,1,0,0,0,"
              "'2026-09-19 00:00:00',NULL)")
    for i, (n, ab) in enumerate([("甲", 0), ("乙", 0), ("丙", 0), ("弃票", 1)]):
        c.execute("INSERT INTO poll_options VALUES (?,?,?,?,?)", (i + 1, 1, n, i, ab))
    c.execute("INSERT INTO voters VALUES (1,1,'张三',2,1,'2026-09-19 10:00:00')")
    c.execute("INSERT INTO voters VALUES (2,1,'李四',3,0,NULL)")
    c.execute("INSERT INTO ballots VALUES (1,1,1,2,'2026-09-19 10:00:00')")
    conn.commit()
    conn.close()


print("\n=== A. 造旧库并启动新版本 ===")
build_legacy_db()
print("  旧库：%s" % DB_PATH)

from app import app  # noqa: E402  导入即触发 create_all + ensure_schema_upgrades
from models import Ballot, Poll, PollOption, User, Voter, db  # noqa: E402

from sqlalchemy import inspect, text  # noqa: E402

with app.app_context():
    insp = inspect(db.engine)
    poll_cols = {c["name"] for c in insp.get_columns("polls")}
    voter_cols = {c["name"] for c in insp.get_columns("voters")}
    ddl = db.session.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ballots'"
    )).scalar() or ""
    n_ballots = db.session.execute(text("SELECT COUNT(*) FROM ballots")).scalar()
    n_voters = db.session.execute(text("SELECT COUNT(*) FROM voters")).scalar()
    legacy_poll = db.session.get(Poll, 1)
    legacy_n_options = len(legacy_poll.options) if legacy_poll else 0
    legacy_n_voters = len(legacy_poll.voters) if legacy_poll else 0
    legacy_quota = int(legacy_poll.votes_per_voter or 0) if legacy_poll else 0

print("\n--- 迁移结果 ---")
check("polls 表已补出 votes_per_voter 列", "votes_per_voter" in poll_cols, sorted(poll_cols))
check("voters 表已补出 abstained 列", "abstained" in voter_cols, sorted(voter_cols))
check("ballots 旧唯一约束已移除", "uq_ballot_poll_voter " not in ddl and "\n    CONSTRAINT uq_ballot_poll_voter UNIQUE" not in ddl)
check("ballots 新唯一约束已建立", "uq_ballot_poll_voter_option" in ddl)
check("旧选票数据完好", n_ballots == 1 and n_voters == 2, "ballots=%s voters=%s" % (n_ballots, n_voters))
check("旧投票读取正常（votes_per_voter 默认 1）", legacy_poll is not None and legacy_quota == 1, legacy_quota)
check("旧投票的选项/名单可正常读取", legacy_n_options == 4 and legacy_n_voters == 2,
      "options=%s voters=%s" % (legacy_n_options, legacy_n_voters))

r = app.test_client().get("/healthz")
hz = r.get_json()
check("/healthz 报告结构升级成功", hz.get("status") == "ok", hz)
check("/healthz 记录了本次升级动作",
      any("votes_per_voter" in s for s in hz.get("schema_upgrades", [])),
      hz.get("schema_upgrades"))

# ---------------------------------------------------------------------------
# B. 端到端
# ---------------------------------------------------------------------------
print("\n=== B. 新功能端到端 ===")
client = app.test_client()


def logout():
    client.get("/logout")


def login(name, pwd):
    logout()
    return client.post("/login", data={"real_name": name, "password": pwd},
                       follow_redirects=True)


r = login("admin", "muyu123")
check("admin / muyu123 能登录", "我的主页" in r.get_data(as_text=True))

# 注册王五（张三、李四旧库已有）
logout()
r = client.post("/register",
                data={"real_name": "王五", "password": "123", "password2": "123"},
                follow_redirects=True)
check("王五注册成功", "注册成功" in r.get_data(as_text=True))

# --- admin 创建「每人 7 票」的投票 ---
login("admin", "muyu123")
candidates = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十"]
r = client.post("/poll/create", data={
    "title": "多票测试投票",
    "description": "每人 7 票",
    "votes_per_voter": "7",
    "allow_abstain": "on",
    "allow_self_vote": "no",
    "options": candidates,
    "voter_names": "张三\n李四\n王五",
}, follow_redirects=True)
check("创建 7 票投票成功", "投票创建成功" in r.get_data(as_text=True))

with app.app_context():
    pid = Poll.query.filter_by(title="多票测试投票").first().id
    poll = db.session.get(Poll, pid)
    opt_id = {o.name: o.id for o in poll.options}
print("  poll_id = %s，选项 = %s" % (pid, list(opt_id)))

# --- 超出上限的票数应被拒绝（7 票但只有 7 个可投对象 = 刚好；试 8 票）---
login("admin", "muyu123")
r = client.post("/poll/create", data={
    "title": "超限投票", "votes_per_voter": "8", "allow_self_vote": "no",
    "options": candidates, "voter_names": "张三",
}, follow_redirects=True)
check("票数超过可投对象时被拒绝", "可投对象最多只有" in r.get_data(as_text=True))

# --- 张三：看不到自己 ---
login("张三", "123")
r = client.get("/poll/%d/vote" % pid, follow_redirects=True)
body = r.get_data(as_text=True)
check("张三能打开投票页", "本次每人有" in body)
check("张三看不到与自己同名的选项",
      'value="%s"' % opt_id["张三"] not in body,
      "含自己的选项 id=%s" % opt_id["张三"])

zhang_targets = ["李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十"]

# --- 少投（6 票）应被拒绝 ---
r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id[n]) for n in zhang_targets[:6]]},
                follow_redirects=True)
check("只投 6 票（未投满）被拒绝", "必须投满 7 票" in r.get_data(as_text=True))

# --- 重复投给同一人被拒绝（同一 id 传 7 次）---
r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id["李四"])] * 7},
                follow_redirects=True)
check("重复投给同一个人被拒绝", "必须投满 7 票" in r.get_data(as_text=True))

# --- 绕过前端硬塞自己 ---
r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id["张三"])] + [str(opt_id[n]) for n in zhang_targets[:6]]},
                follow_redirects=True)
check("绕过前端投给自己被拒绝", "不允许投给自己" in r.get_data(as_text=True))

# --- 弃票 + 同时带票 = 拒绝 ---
r = client.post("/poll/%d/vote" % pid,
                data={"abstain": "1", "option_ids": [str(opt_id["李四"])]},
                follow_redirects=True)
check("弃票同时带票被拒绝", "无法再投出任何一票" in r.get_data(as_text=True))

# --- 张三正常投票 ---
r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id[n]) for n in zhang_targets]},
                follow_redirects=True)
body = r.get_data(as_text=True)
check("张三 7 票提交成功", "投票成功，共投出 7 票" in body)
check("张三能看到自己投给了谁", "李四、王五、赵六、钱七、孙八、周九、吴十" in body)

r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id[n]) for n in zhang_targets]},
                follow_redirects=True)
check("张三重复提交被拒绝", "你已经投过票了" in r.get_data(as_text=True))

with app.app_context():
    zhang_v_id = Voter.query.filter_by(poll_id=pid, name="张三").first().id
    n_zhang = Ballot.query.filter_by(poll_id=pid, voter_id=zhang_v_id).count()
check("张三在库里正好 7 条选票", n_zhang == 7, "实际 %s" % n_zhang)

# --- 李四：弃票 ---
r = login("李四", "123")
r = client.post("/poll/%d/vote" % pid, data={"abstain": "1"}, follow_redirects=True)
body = r.get_data(as_text=True)
check("李四弃票成功", "已提交弃票" in body or "弃票" in body)
check("弃票后页面显示未投出任何票", "没有投出任何一票" in body)

r = client.post("/poll/%d/vote" % pid, data={"abstain": "1"}, follow_redirects=True)
check("弃票后不能再次提交", "你已经投过票了" in r.get_data(as_text=True))

with app.app_context():
    li_v = Voter.query.filter_by(poll_id=pid, name="李四").first()
    check("弃票者库里 0 条选票但已标记投票",
          Ballot.query.filter_by(poll_id=pid, voter_id=li_v.id).count() == 0
          and bool(li_v.has_voted) and bool(li_v.abstained))

# --- 王五：正常投 7 票 ---
r = login("王五", "123")
wang_targets = ["张三", "李四", "赵六", "钱七", "孙八", "周九", "吴十"]
r = client.post("/poll/%d/vote" % pid,
                data={"option_ids": [str(opt_id[n]) for n in wang_targets]},
                follow_redirects=True)
check("王五 7 票提交成功", "投票成功，共投出 7 票" in r.get_data(as_text=True))

# --- 发起人管理页 & 导出 ---
r = login("admin", "muyu123")
r = client.get("/poll/%d/manage" % pid, follow_redirects=True)
check("管理页可打开且出现「导出 Excel」", "导出 Excel" in r.get_data(as_text=True))

r = client.get("/poll/%d/status" % pid)
st = r.get_json()
counts = {o["name"]: o["count"] for o in st["options"]}
print("  票数：%s" % counts)
check("票数统计正确（李四/赵六/钱七/孙八/周九/吴十各 2 票，张三/王五各 1 票）",
      counts.get("李四") == 2 and counts.get("赵六") == 2 and counts.get("张三") == 1
      and counts.get("王五") == 1 and "弃票" not in counts, counts)
check("status 返回每人票数", st.get("votes_per_voter") == 7, st.get("votes_per_voter"))
check("status 正确统计弃票人数", st.get("abstain_count") == 1, st.get("abstain_count"))
check("status 已投票列表里李四被标为弃票",
      any(v["name"] == "李四" and v["abstained"] for v in st["voted"]))

# --- 导出 Excel ---
r = client.get("/poll/%d/export" % pid)
check("导出接口返回 200 xlsx", r.status_code == 200 and r.data[:2] == b"PK",
      "status=%s len=%s" % (r.status_code, len(r.data)))
print("  下载文件名头：%s" % r.headers.get("Content-Disposition", "")[:120])

from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(io.BytesIO(r.data))
print("  工作表：%s" % wb.sheetnames)

ws = wb["得票统计"]
rows = [row for row in ws.iter_rows(values_only=True)]
head = next(i for i, row in enumerate(rows) if row and row[0] == "名次")
data_rows = [row for row in rows[head + 1:] if row and row[0] not in (None, "")]
rank = {row[1]: (row[0], row[2]) for row in data_rows}
print("  名次表：%s" % rank)
check("得票数降序排列", [r[2] for r in data_rows if isinstance(r[2], int)] ==
      sorted([r[2] for r in data_rows if isinstance(r[2], int)], reverse=True))
check("并列票数同名次（6 人 2 票并列第 1）",
      rank.get("李四", (None,))[0] == 1 and rank.get("赵六", (None,))[0] == 1
      and rank.get("吴十", (None,))[0] == 1)
check("并列之后名次正确跳到 7",
      rank.get("张三", (None,))[0] == 7 and rank.get("王五", (None,))[0] == 7)
check("弃票单独一行标注", any("弃票" in str(row[1]) for row in data_rows))

ws2 = wb["投票明细"]
rows2 = [row for row in ws2.iter_rows(values_only=True)]
head2 = {c: i for i, c in enumerate(rows2[0])}
detail = {row[head2["姓名"]]: row for row in rows2[1:]}
print("  明细：%s" % {k: (v[head2["状态"]], v[head2["投票时间"]], v[head2["投给了谁"]]) for k, v in detail.items()})
check("明细含投票时间", bool(detail["张三"][head2["投票时间"]]))
check("明细含「投给了谁」且为 7 人",
      len(str(detail["张三"][head2["投给了谁"]]).split("、")) == 7,
      detail["张三"][head2["投给了谁"]])
check("明细里弃票被标注", detail["李四"][head2["状态"]] == "弃票")

ws3 = wb["投票矩阵"]
rows3 = [row for row in ws3.iter_rows(values_only=True)]
head3 = {str(c): i for i, c in enumerate(rows3[0]) if c}
matrix = {row[0]: row for row in rows3[1:]}
check("矩阵里张三勾了 7 个候选人",
      sum(1 for n in candidates if matrix["张三"][head3[n]] == "√") == 7)
check("矩阵里张三没勾自己（张三列有票但来自王五）",
      not matrix["张三"][head3["张三"]],
      "张三行=%s" % (matrix["张三"],))
check("矩阵里王五勾了张三（他的票里有张三）",
      matrix["王五"][head3["张三"]] == "√")

# --- 旧投票仍然可用（1 票单选兼容）---
r = login("admin", "muyu123")
r = client.get("/poll/1/manage", follow_redirects=True)
check("旧投票管理页正常打开（老数据无 votes_per_voter 也不报错）",
      r.status_code == 200 and "旧版单选投票" in r.get_data(as_text=True))
r = client.get("/poll/1/export")
check("旧投票也能导出", r.status_code == 200 and r.data[:2] == b"PK")

# 旧投票是单选（quota=1），李四还没投过 —— 走一遍旧体验
r = login("李四", "123")
r = client.get("/poll/1/vote", follow_redirects=True)
body = r.get_data(as_text=True)
check("旧投票按 1 票渲染", "本次每人有 <b>1</b> 票" in body)
check("旧投票的「弃票选项」不再出现在候选里，改由独立弃票入口承担",
      "name=\"option_ids\"" in body and body.count("name=\"option_ids\"") == 3,
      "候选数=%s" % body.count('name="option_ids"'))
r = client.post("/poll/1/vote", data={"option_ids": [str(1)]}, follow_redirects=True)
check("旧投票单选提交成功", "投票成功，共投出 1 票" in r.get_data(as_text=True))


def poll_id_by_title(t):
    with app.app_context():
        p = Poll.query.filter_by(title=t).first()
        return p.id if p else None


# --- 未开启弃票的投票：不能弃票 ---
login("admin", "muyu123")
client.post("/poll/create", data={
    "title": "无弃票投票", "votes_per_voter": "1", "allow_self_vote": "no",
    "options": ["甲", "乙"], "voter_names": "张三",
}, follow_redirects=True)
p_no_ab = poll_id_by_title("无弃票投票")
login("张三", "123")
r = client.post("/poll/%d/vote" % p_no_ab, data={"abstain": "1"}, follow_redirects=True)
check("未开启弃票时提交弃票被拒绝", "不允许弃票" in r.get_data(as_text=True))

# --- 允许投自己：可以投给自己 ---
login("admin", "muyu123")
client.post("/poll/create", data={
    "title": "可投自己投票", "votes_per_voter": "1", "allow_self_vote": "yes",
    "options": ["张三", "李四"], "voter_names": "张三",
}, follow_redirects=True)
p_self = poll_id_by_title("可投自己投票")
with app.app_context():
    self_id = {o.name: o.id for o in db.session.get(Poll, p_self).options}
login("张三", "123")
r = client.get("/poll/%d/vote" % p_self, follow_redirects=True)
check("允许投自己时选项里能看到自己", 'value="%s"' % self_id["张三"] in r.get_data(as_text=True))
r = client.post("/poll/%d/vote" % p_self, data={"option_ids": [str(self_id["张三"])]},
                follow_redirects=True)
check("允许投自己时可以投给自己", "投票成功，共投出 1 票" in r.get_data(as_text=True))

# --- 第二次启动：迁移必须幂等，不重复改表、不报错 ---
import subprocess  # noqa: E402

p = subprocess.run([sys.executable, "-c", "import app; print('SECOND_BOOT_OK')"],
                   cwd=ROOT, env=dict(os.environ), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
check("第二次启动（迁移幂等、无异常）", "SECOND_BOOT_OK" in (p.stdout or ""),
      ((p.stdout or "")[-200:] + (p.stderr or "")[-300:]))

with app.app_context():
    ddl2 = db.session.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ballots'"
    )).scalar() or ""
    n2 = db.session.execute(text("SELECT COUNT(*) FROM ballots")).scalar()
check("二次启动后选票数据未受影响", n2 >= 16, "ballots=%s" % n2)
check("二次启动后约束仍是新版本", "uq_ballot_poll_voter_option" in ddl2)

# --- 全新空库：不能因为迁移逻辑报错 ---
fresh_db = os.path.join(TMP, "fresh.db")
env_fresh = dict(os.environ)
env_fresh["DATABASE_URL"] = "sqlite:///" + fresh_db.replace("\\", "/")
p = subprocess.run(
    [sys.executable, "-c",
     "import app; c=app.app.test_client(); print('FRESH:', c.get('/healthz').get_json())"],
    cwd=ROOT, env=env_fresh, capture_output=True, text=True, encoding="utf-8", errors="replace")
check("全新空库可直接启动（无需先有表）",
      "FRESH:" in (p.stdout or "") and "'status': 'ok'" in (p.stdout or ""),
      ((p.stdout or "")[-200:] + (p.stderr or "")[-300:]))

# --- 极端输入：字段超长必须友好拒绝，不能 500（线上曾实测出 5 处 500）---
print("\n=== I. 极端输入（字段超长）===")
logout()
r = client.post("/register", data={"real_name": "超长" + "测" * 80, "password": "abc123",
                                   "password2": "abc123"}, follow_redirects=True)
check("超长姓名（84 字）注册被友好拒绝",
      r.status_code == 200 and "过长" in r.get_data(as_text=True), r.status_code)
r = client.post("/register", data={"real_name": "临时长密码", "password": "a" * 300,
                                   "password2": "a" * 300}, follow_redirects=True)
check("超长密码（300 位）注册被友好拒绝",
      r.status_code == 200 and "过长" in r.get_data(as_text=True), r.status_code)

login("admin", "muyu123")
r = client.post("/poll/create", data={"title": "超长" + "题" * 300, "votes_per_voter": "1",
                                      "options": ["甲", "乙"], "voter_names": "王五"},
                follow_redirects=True)
check("超长投票标题（304 字）被友好拒绝",
      r.status_code == 200 and "过长" in r.get_data(as_text=True), r.status_code)
r = client.post("/poll/create", data={"title": "超长选项场次", "votes_per_voter": "1",
                                      "options": ["选" * 300, "乙"], "voter_names": "王五"},
                follow_redirects=True)
check("超长选项名（302 字）被友好拒绝",
      r.status_code == 200 and "过长" in r.get_data(as_text=True), r.status_code)
r = client.post("/poll/create", data={"title": "超长名单场次", "votes_per_voter": "1",
                                      "options": ["甲", "乙"],
                                      "voter_names": "王五\n" + "名" * 80},
                follow_redirects=True)
body = r.get_data(as_text=True)
check("名单里的超长姓名被忽略并提示（投票仍创建成功）",
      "投票创建成功" in body and "忽略" in body,
      body[body.find("flash"):body.find("flash") + 120] if "flash" in body else body[:80])

# --- 修改密码 ---
print("\n=== J. 修改密码 ===")


def pwd_after_restart(db_env, check_pwd):
    """起一个新进程（模拟 Render 重启/唤醒），看 admin 的密码变成什么。"""
    code = ("import app;from models import User;"
            "app.app.app_context().push();"
            "u=User.query.filter_by(real_name='admin').first();"
            "print('OK' if u.check_password('%s') else 'RESET')" % check_pwd)
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=db_env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "").strip()


logout()
login("张三", "123")
r = client.get("/change_password", follow_redirects=True)
check("J1 登录后能打开改密码页", "修改密码" in r.get_data(as_text=True))

r = client.post("/change_password", data={"old_password": "wrongpwd", "new_password": "new123",
                                          "new_password2": "new123"}, follow_redirects=True)
check("J2 原密码错误被拒绝", "原密码不正确" in r.get_data(as_text=True))
r = client.post("/change_password", data={"old_password": "123", "new_password": "new123",
                                          "new_password2": "new456"}, follow_redirects=True)
check("J3 两次新密码不一致被拒绝", "不一致" in r.get_data(as_text=True))
r = client.post("/change_password", data={"old_password": "123", "new_password": "new!@#",
                                          "new_password2": "new!@#"}, follow_redirects=True)
check("J4 新密码含非法字符被拒绝", "只能使用大小写字母和数字" in r.get_data(as_text=True))
r = client.post("/change_password", data={"old_password": "123", "new_password": "123",
                                          "new_password2": "123"}, follow_redirects=True)
check("J5 新密码与原密码相同被拒绝", "不能与原密码相同" in r.get_data(as_text=True))
r = client.post("/change_password", data={"old_password": "", "new_password": "new123",
                                          "new_password2": "new123"}, follow_redirects=True)
check("J6 原密码为空被拒绝", "不能为空" in r.get_data(as_text=True))
r = client.post("/change_password", data={"old_password": "123", "new_password": "a" * 200,
                                          "new_password2": "a" * 200}, follow_redirects=True)
check("J7 新密码超长被拒绝（不 500）", "过长" in r.get_data(as_text=True))

r = client.post("/change_password", data={"old_password": "123", "new_password": "new123",
                                          "new_password2": "new123"}, follow_redirects=True)
check("J8 正常修改密码成功", "密码修改成功" in r.get_data(as_text=True))
logout()
r = client.post("/login", data={"real_name": "张三", "password": "new123"}, follow_redirects=True)
check("J9 新密码可以登录", "我的主页" in r.get_data(as_text=True))
logout()
r = client.post("/login", data={"real_name": "张三", "password": "123"}, follow_redirects=True)
check("J10 旧密码已失效", "姓名或密码错误" in r.get_data(as_text=True))
logout()
r = client.get("/change_password", follow_redirects=True)
check("J11 未登录访问改密码页被引导登录", "请先登录" in r.get_data(as_text=True))

# admin 改密码：未设环境变量时应持久生效
login("admin", "muyu123")
r = client.post("/change_password", data={"old_password": "muyu123", "new_password": "admin999",
                                          "new_password2": "admin999"}, follow_redirects=True)
check("J12 admin 可以改自己的密码", "密码修改成功" in r.get_data(as_text=True))
check("J13 未设环境变量时，重启后 admin 新密码仍生效（不被覆盖回默认值）",
      pwd_after_restart(dict(os.environ), "admin999") == "OK",
      pwd_after_restart(dict(os.environ), "admin999"))

# 设了环境变量：以环境变量为准（忘记密码时的找回通道）
env_force = dict(os.environ)
env_force["ADMIN_PASSWORD"] = "envpwd888"
pwd_after_restart(env_force, "envpwd888")
check("J14 设了 ADMIN_PASSWORD 时，重启会把密码校正回环境变量的值",
      pwd_after_restart(env_force, "envpwd888") == "OK")

env_back = dict(os.environ)
env_back["ADMIN_PASSWORD"] = "muyu123"
pwd_after_restart(env_back, "muyu123")
check("J15 用环境变量可把 admin 密码找回（恢复现场）",
      pwd_after_restart(env_back, "muyu123") == "OK")

# ---------------------------------------------------------------------------
print("\n=== 汇总 ===")
print("  通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  ! %s" % f)
print("  临时库：%s" % DB_PATH)
sys.exit(1 if FAIL else 0)
