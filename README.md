# 匿名投票系统

基于 Flask + PostgreSQL 的匿名投票程序，可一键部署到 [Render](https://render.com)。
UI 采用中国农业大学经典绿。

## 功能一览

- **注册/登录**：注册时提示使用真实姓名；用户名唯一，与密码一一对应。
- **发起投票**：自定义选项名称与数量；可选是否设「弃票」；可选投票者「能否投给自己」（默认否）。
- **名单导入**：文本粘贴，或导入 Excel/CSV（取第一列姓名）；图片名单需手动录入。发起人自动被排除，**不可参与自己发起的投票**。
- **匿名规则**：投票者只能看到自己的选择，无法查看他人选择、各选项票数或整体结果；**发起人可查看明细**，并可在管理页一键切换「显示/隐藏 选项后的投票者姓名」。
- **结束与公布**：发起人手动结束投票；结束后可选择公布/取消公布票数。
- **实时进度**：发起人管理页自动刷新（每 4 秒轮询），实时显示已投/未投名单，无需手动刷新。

## 匿名性说明

- 对**参与者**：完全匿名，任何投票者都看不到谁投了谁。
- 对**发起人 / 数据库管理员**：可追溯（数据库中 `ballots` 表记录了投票者与所投选项的关联）。这是你确认的口径「对参与者匿名，发起人可看明细」。

---

## 一键部署到 Render（推荐用 Blueprint）

项目已包含 `render.yaml`，会自动创建 Web 服务 + 免费 PostgreSQL 数据库。

### 步骤

1. **把代码推到 GitHub**（在本项目目录下执行）：
   ```bash
   git init
   git add .
   git commit -m "anonymous vote app"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

2. **登录 Render** → 右上角 **New +** → **Blueprint**。

3. 选择刚才推送的 GitHub 仓库 → Render 会自动读取 `render.yaml`，
   识别出一个 Web 服务和一个 PostgreSQL 数据库 → 点 **Apply**。

4. 等待构建完成（首次约 2–4 分钟）。`SECRET_KEY` 会自动生成，
   `DATABASE_URL` 会自动从数据库注入，**无需手动配置环境变量**。

5. 部署成功后，访问 Render 给出的网址（形如 `https://anonymous-vote.onrender.com`）即可使用。

### 使用顺序建议

1. 每位参与者先各自**注册账号**（务必填**真实姓名**）。
2. 发起人注册后**发起投票**，粘贴或导入参与者**真实姓名**名单。
3. 系统按真实姓名把名单与已注册账号自动关联；参与者登录后在「邀请我参与的投票」中看到并投票。
4. 发起人在管理页实时查看进度，结束后按需公布票数。

> 名单里带 `*` 号的姓名表示该人尚未注册对应账号；等对方用相同真实姓名注册后会自动关联。

---

## 备选：手动部署（不用 Blueprint）

1. Render → New + → **PostgreSQL**，plan 选 Free，创建后复制 **Internal Database URL**。
2. Render → New + → **Web Service**，连接同一 GitHub 仓库：
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
   - Environment 添加：
     - `DATABASE_URL` = 上一步复制的 Internal Database URL
     - `SECRET_KEY` = 任意长随机字符串
     - `PYTHON_VERSION` = `3.11.9`
3. Create Web Service，等待部署完成。

---

## 本地运行（可选）

```bash
pip install -r requirements.txt
python app.py
# 访问 http://127.0.0.1:5000
```

本地默认使用 SQLite（`vote.db`），无需数据库。设置 `DATABASE_URL` 环境变量即可切换到 PostgreSQL。

---

## 技术栈

- Python 3.11 / Flask 3
- Flask-SQLAlchemy + PostgreSQL（本地回退 SQLite）
- Gunicorn（生产 WSGI 服务器）
- 前端：原生 HTML/CSS/JS，无构建步骤

## 文件结构

```
├── app.py              # 主应用（路由、认证、投票、名单、状态接口）
├── models.py           # 数据模型
├── config.py           # 配置（DATABASE_URL / SECRET_KEY）
├── requirements.txt    # 依赖
├── render.yaml         # Render Blueprint（Web + 数据库）
├── Procfile            # 启动命令
├── runtime.txt         # Python 版本
├── static/style.css    # 中国农业大学经典绿主题
└── templates/          # 页面模板
```
