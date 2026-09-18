from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model):
    """用户：用户名唯一，真实姓名，密码一一对应。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    real_name = db.Column(db.String(64), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Poll(db.Model):
    """一场投票。"""

    __tablename__ = "polls"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    creator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    allow_self_vote = db.Column(db.Boolean, default=False)   # 投票者能否投给自己，默认否
    allow_abstain = db.Column(db.Boolean, default=False)     # 是否设置弃票选项
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

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("poll_id", "name", name="uq_voter_poll_name"),
    )


class Ballot(db.Model):
    """选票记录：记录谁投给了哪个选项。

    对普通参与者永远不可见；发起人可在自己页面选择显示/隐藏。
    """

    __tablename__ = "ballots"

    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("polls.id"), nullable=False, index=True)
    voter_id = db.Column(db.Integer, db.ForeignKey("voters.id"), nullable=False, index=True)
    option_id = db.Column(db.Integer, db.ForeignKey("poll_options.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voter = db.relationship("Voter")
    option = db.relationship("PollOption")

    __table_args__ = (
        db.UniqueConstraint("poll_id", "voter_id", name="uq_ballot_poll_voter"),
    )
