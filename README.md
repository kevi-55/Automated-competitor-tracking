# Automated Competitor Tracking

这个项目会每天自动追踪竞品网站变化，并把日报发到邮箱；每周一再发一封周报。

## 第二版：准确性优先

当前版本不再把“第一次抓到”直接等同于“今天发布”，而是分别记录：

- `published_at`：页面实际发布时间
- `first_seen_at`：系统首次发现时间
- `sitemap_lastmod`：站点地图声明的最后更新时间
- `discovered_via`：RSS、sitemap、重点页或页面链接

主要改进：

- 优先从 RSS/Atom 获取最新文章和发布时间，再使用 sitemap 与栏目页补充。
- 超过 `new_content_max_age_days` 才发现的文章标记为“历史内容补录”，不计入今日新内容。
- 提取 JSON-LD、文章 Meta 和正文日期，邮件同时显示发布时间与发现时间。
- 先过滤 Cookie、聊天工具、浏览次数、评论数等动态噪声，再计算内容变化。
- 邮件直接显示绿色“新增”和红色“删除”片段，不再只显示两段难以理解的摘要。
- HTTP 错误页不会覆盖上一次正确快照；规范 URL 和跳转 URL 会去重。
- 监控页面数量骤降或抓取失败率过高时，单独发送监控健康告警。
- 每次运行生成带 `run_id` 的报告，同一天手动运行不会覆盖旧报告。
- 数据状态会在邮件发送前落盘；即使邮件服务失败，GitHub Actions 也会保存状态。

从旧版本升级后的第一次运行会静默迁移旧快照，避免因为提取算法升级产生大批伪变化。

当前追踪对象：

- Hubs
- Xometry
- Fictiv
- Protolabs
- JLC CNC
- PCBWay
- eMachineShop
- SyBridge
- CNCLATHING

重点关注：

- 首页文案
- 服务页
- pricing / price / quote 相关页面
- 产品页 / capability 页面
- blog / resources / articles / changelog / news
- 新增页面
- H1、SEO 标题、Meta description
- 主要板块文案
- CTA 文案
- 重点页桌面和移动端 UI 截图变化

默认忽略：

- Cookie 弹窗
- 招聘 / career / jobs 页面
- 隐私、条款、登录、购物车、账号页等低价值页面

## 自动运行时间

GitHub Actions 使用 UTC 时间。当前配置等价于北京时间：

- 日报：每天 09:07
- 周报：每周一 09:37

没有设成整点，是为了避开 GitHub Actions 定时任务高峰，稳定性更好。

## 你需要配置的 GitHub Secrets

进入你的仓库：

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

### 方案 A：Resend

推荐，配置最少。

必填：

- `MAIL_TO`：`kevi.rapiddirect@gmail.com`
- `MAIL_FROM`：测试可以用 `Competitor Tracker <onboarding@resend.dev>`；长期建议换成你已验证域名下的邮箱
- `RESEND_API_KEY`：你的 Resend API Key

注意：不要把 API key 写进代码、README 或聊天记录。只放到 GitHub Secrets。如果 key 泄露过，先在 Resend 删除旧 key，再生成新的 key。

### 方案 B：Gmail SMTP

必填：

- `MAIL_TO`：`kevi.rapiddirect@gmail.com`
- `MAIL_FROM`：你的 Gmail 地址
- `SMTP_HOST`：`smtp.gmail.com`
- `SMTP_PORT`：`587`
- `SMTP_USER`：你的 Gmail 地址
- `SMTP_PASS`：Gmail App Password，不是 Gmail 登录密码

Gmail App Password 需要你的 Google 账号开启两步验证。

## 第一次怎么跑

第一次运行会建立基线。它会把当前页面状态保存下来，所以第一封日报可能会出现大量“新发现页面”。

步骤：

1. 把本项目所有文件上传到你的 GitHub 仓库。
2. 配好 Secrets。
3. 打开 GitHub 仓库的 `Actions`。
4. 选择 `Daily competitor tracking`。
5. 点击 `Run workflow`。

第一次成功后，仓库里会出现：

- `.tracker_state/`：页面快照、截图基线
- `reports/`：每天和每周的 HTML / JSON 报告

后续每天会自动对比上一次状态。

## 本地测试

如果你想在本地测试：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s tests -v
$env:MAIL_TO="kevi.rapiddirect@gmail.com"
PYTHONPATH=src python -m tracker.main run --mode daily --config config/competitors.yml
```

没有配置邮件密钥时，程序仍会生成报告，只是不会发邮件。

## 调整监控范围

修改：

`config/competitors.yml`

常用项：

- `max_pages_per_domain`：每个竞品最多追踪多少页面
- `screenshot_priority_pages_per_domain`：每个竞品多少个重点页做 UI 截图对比
- `priority_paths`：每个竞品的重点页面
- `include_path_keywords`：自动发现时保留哪些 URL
- `exclude_path_keywords`：自动发现时排除哪些 URL

如果某个竞品页面很多，先调高它的重点页比盲目加大全站页数更有效。
