import os


class Config:
    """应用配置。生产环境从环境变量读取；本地默认用 SQLite。"""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-prod")

    # ---- 会话 Cookie 安全配置 ----
    # 让每个浏览器只认自己的 session，避免多人同时在线时相互串号。
    SESSION_COOKIE_HTTPONLY = True          # 禁止 JS 读取，防 XSS 窃取
    SESSION_COOKIE_SAMESITE = "Lax"         # 限制跨站携带，降低串号/CSRF 风险
    # 生产（HTTPS）下仅通过安全连接传输 Cookie；本地 http 调试保持关闭。
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # Render 提供的 DATABASE_URL 形如 postgres://...，SQLAlchemy 2.x 需要 postgresql://
    _db_url = os.environ.get("DATABASE_URL", "")
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url or "sqlite:///vote.db"

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,      # 取连接前探活，避免使用已被数据库关闭的死连接
        "pool_recycle": 300,        # 5 分钟回收，规避云数据库空闲断连
        # ---- 连接池：配合 gunicorn 多 worker + 多线程 ----
        # 单个 worker 的连接上限 = pool_size + max_overflow。
        # 默认 5+5=10，配合 2 worker 共 ~20 连接，既能撑住 60 人并发操作，
        # 又不超过 Render free PostgreSQL 的连接上限。可用环境变量微调。
        # 连接池：配合 2 worker，每 worker 上限 = pool_size + max_overflow = 8，
        # 总计约 16 个连接，控制在 Render free PostgreSQL 连接上限（~20）内。
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "4")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "4")),
        "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT", "30")),
    }

    # 上传文件大小限制（名单图片/Excel）
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8MB
