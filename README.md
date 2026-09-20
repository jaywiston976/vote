# 匿名投票系统

> **当前版本：多票版（2026-09-20）** — 每人可自定义票数（必须投给不同的人）、弃票即一票不投、
> 禁止投给自己、发起人一键导出 Excel；旧数据库启动时自动升级，历史数据不受影响。

基于 Flask + PostgreSQL 的匿名投票程序，可一键部署到 [Render](https://render.com)。
UI 采用中国农业大学经典绿。

## 功能一览

- **注册/登录**：直接以**真实姓名**作为账号（无单独用户名），姓名全系统唯一，与密码一一对应。
- **管理员**：仅 `admin` 账号（姓名 `admin`，密码见下方「管理员密码」）可发起与管理投票；该姓名为系统保留，不可被注册占用。
- **发起投票**：自定义选项名称与数量；可设置**每人拥有的票数**（如每人 7 票）；可选是否允许「弃票」；可选投票者「能否投给自己」（默认否）。
- **多票规则**：每人必须**投满**设定票数才允许提交，且**必须投给不同的人**——同一个人不能被重复投两次（数据库层也有唯一约束兜底）。
- **不能投自己**：关闭「投给自己」时，选项名与本人真实姓名一致的选项会从该用户的投票页**直接隐藏**；绕过前端强行提交同样会被后端拒绝（姓名比对忽略空格与大小写）。
- **弃票**：选择弃票即**一票都不投**（不产生任何选票记录），弃票与投票互斥、提交后不可修改；发起人页面的已投名单与导出表格都会明确标注「弃票」。
- **名单导入**：文本粘贴，或导入 Excel/CSV（取第一列姓名）；图片名单需手动录入。发起人自动被排除，**不可参与自己发起的投票**。
- **匿名规则**：投票者只能看到自己的选择，无法查看他人选择、各选项票数或整体结果；**发起人可查看明细**，并可在管理页一键切换「显示/隐藏 选项后的投票者姓名」。
- **导出 Excel（仅发起人）**：管理页一键下载 xlsx，含三张表——
  - **得票统计**：按得票数从高到低排列，**票数并列的名次也并列**（如 6 人同为 2 票则并列第 1，下一名从第 7 开始）；弃票单独一行，不计入任何人的得票。
  - **投票明细**：每人一行，记录姓名、状态（已投票 / 弃票 / 未投票）、投票时间（北京时间）、投给了谁。
  - **投票矩阵**：行 = 投票人，列 = 候选人，打勾显示对应关系。
- **结束与公布**：发起人手动结束投票；结束后可选择公布/取消公布票数。
- **实时进度**：发起人管理页自动刷新（每 4 秒轮询），实时显示已投 / 未投 / 弃票人数，无需手动刷新。

## 管理员密码

以真实姓名 `admin` 登录。密码**不**从数据库读取，而是每次应用启动时由
`app.py` 中的 `ADMIN_PASSWORD` 强制校正，因此：

- 想改密码：改 `ADMIN_PASSWORD`，不要只改数据库（会被下次启动覆盖）。
- 推荐做法：在 Render 面板设环境变量 `ADMIN_PASSWORD`，改完会自动重新部署并生效，不用动代码。
- 未设该环境变量时，使用代码里的默认值。

## 升级旧版本？（数据会不会冲突）

**可以直接覆盖上线，历史数据不会丢、也不会冲突**，但**必须带上 `app.py`**。
原因：`db.create_all()` 只建不存在的整张表，**不会给已存在的表补新列**。新版本
新增了字段和约束，如果只改 `models.py` 不带迁移，线上旧库会直接报错。

`app.py` 里的 `ensure_schema_upgrades()` 会在每次启动时做幂等迁移（可反复执行、不清任何数据）：

| 变更 | 迁移动作 |
|---|---|
| `polls` 新增 `votes_per_voter` | `ALTER TABLE polls ADD COLUMN votes_per_voter INTEGER DEFAULT 1` |
| `voters` 新增 `abstained` | `ALTER TABLE voters ADD COLUMN abstained BOOLEAN DEFAULT FALSE` |
| `ballots` 唯一约束放宽 | `(poll_id, voter_id)` → `(poll_id, voter_id, option_id)`，否则第二票会被直接拦住 |

兼容性要点：

- 旧投票的 `votes_per_voter` 一律补 `1`，**行为与升级前完全一致**（单选）。
- 旧投票里的「弃票」是一个真实选项；新版会把它正确识别为弃票（管理页、导出表均标注弃票），不会算成投给某人的票。
- 旧投票的投票记录、名单、已投票状态全部保留。
- 建议上线前先导出一份线上数据留档：`DATABASE_URL="<Render 的 External Database URL>" python export_results.py`。

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
├── app.py                  # 主应用（认证、投票、名单、状态接口、Excel 导出、旧库自动迁移）
├── models.py               # 数据模型（User / Poll / PollOption / Voter / Ballot）
├── config.py               # 配置（DATABASE_URL / SECRET_KEY / 连接池）
├── requirements.txt        # 依赖
├── render.yaml             # Render Blueprint（Web + 数据库）
├── Procfile                # 启动命令
├── runtime.txt             # Python 版本
├── static/style.css        # 中国农业大学经典绿主题
├── templates/              # 页面模板（7 个）
├── export_results.py       # 可选：命令行备份脚本，把线上库导成 xlsx + json 留档
└── test_schema_upgrade.py  # 可选：旧库升级 + 新功能回归验证脚本（56 项断言）
```

> 后两个是工具脚本，不影响部署，也可直接删除。
