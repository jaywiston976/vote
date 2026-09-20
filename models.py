from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash

db = SQLAlchemy()


class User(db.Model):
    """用户：用户名唯一，真实姓名，密码一一对应。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    real_name = db.Column(db.String(64), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 明文密码前缀标记：按用户要求“无需安全防护、只追求极致并发”，
    # 密码以明文保存并直接字符串比对，密码校验开销近乎为 0，
    # 彻底消除 free 实例上密码哈希打满 CPU 导致的登录排队。
    # 注意：这是刻意牺牲安全性换取速度，仅适用于无安全要求的内部场景。
    _PLAIN_PREFIX = "plain:"

    def set_password(self, password: str) -> None:
        self.password_hash = self._PLAIN_PREFIX + (password or "")

    def check_password(self, password: str) -> bool:
        stored = self.password_hash or ""
        if stored.startswith(self._PLAIN_PREFIX):
            # 明文直接比对，几乎不耗 CPU
            return stored[len(self._PLAIN_PREFIX):] == (password or "")
        # 兼容历史哈希账号：仍可登录（登录成功后会被透明转为明文）
        try:
            return check_password_hash(stored, password or "")
        except Exception:
            return False

    @property
    def is_plain(self) -> bool:
        return (self.password_hash or "").startswith(self._PLAIN_PREFIX)


class Poll(db.Model):
    """一场投票。"""

    __tablename__ = "polls"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    creator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    allow_self_vote = db.Column(db.Boolean, default=False)   # 投票者能否投给自己，默认否
    allow_abstain = db.Column(db.Boolean, default=False)     # 是否允许「弃票」（弃票 = 一票不投）
    # 每人拥有的票数：7 表示每人必须投满 7 票，且必须投给 7 个不同的人。
    # 默认 1，等价于历史版本的「单选」，旧数据升级后行为不变。
    votes_per_voter = db.Column(db.Integer, default=1, nullable=False)
    is_closed = db.Column(db.Boolean, default=False)         # 是否已结束
    is_published = db.Column(db.Boolean, default=False)      # 发起人是否公布票数
    show_voter_names = db.Column(db.Boolean, default=False)  # 发起人页面是否显示选项后的投票者名字

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)

    creator = db.relationship("User", backref="polls")
    options = db.relationship(
        "PollOption", backref="poll", cascade="all, delete-orphan", order_by="PollOption.position"
    )
    voters = db.relationship("Voter", backref="poll", cascade="all, delete-orphan")
    ballots = db.relationship("Ballot", backref="poll", cascade="all, delete-orphan")


class PollOption(db.Model):
    """投票选项。"""

    __tablename__ = "poll_options"

    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("polls.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    position = db.Column(db.Integer, default=0)
    is_abstain = db.Column(db.Boolean, default=False)  # 是否是"弃票"选项


class Voter(db.Model):
    """投票者名单（发起人导入或添加）。发起人不在名单中。"""

    __tablename__ = "voters"

    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("polls.id"), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)          # 名单上的姓名
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)  # 匹配到的账号
    has_voted = db.Column(db.Boolean, default=False)
    voted_at = db.Column(db.DateTime, nullable=True)
    # 弃票：该投票者主动选择「一票不投」。此时没有任何 Ballot 记录，
    # 所以判断「是否已投过」必须用 has_voted，不能再用「有没有 ballot」。
    abstained = db.Column(db.Boolean, default=False)

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("poll_id", "name", name="uq_voter_poll_name"),
    )


class Ballot(db.Model):
    """选票记录：记录谁投给了哪个选项。

    一人多票时，同一个人会有多行 Ballot（每投给一个选项一行）。
    对普通参与者永远不可见；发起人可在自己页面选择显示/隐藏，
    也可通过「导出 Excel」拿到完整明细。
    """

    __tablename__ = "ballots"

    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("polls.id"), nullable=False, index=True)
    voter_id = db.Column(db.Integer, db.ForeignKey("voters.id"), nullable=False, index=True)
    option_id = db.Column(db.Integer, db.ForeignKey("poll_options.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voter = db.relationship("Voter")
    option = db.relationship("PollOption")

    # 唯一约束必须是「同一人 + 同一选项」：允许一人投多票，但同一人不能重复
    # 投给同一个人（即必须投给不同的人）。
    # 注意：历史库上这里曾经是 (poll_id, voter_id)，会直接拦住第二票，
    # 升级时由 app.py 的 ensure_schema_upgrades() 自动改掉。
    __table_args__ = (
        db.UniqueConstraint("poll_id", "voter_id", "option_id",
                            name="uq_ballot_poll_voter_option"),
    )
