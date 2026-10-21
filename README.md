# Martin 美股观察池 · 轻量首次发布版

本包仅含网页和更新程序，不含历史行情数据。首次发布必须运行Actions下载真实行情，不支持跳过refresh。

1. 将本目录里面的全部内容上传到GitHub仓库根目录，包括`.github`、`dist`、`scanner_core`和`state`。
2. 仓库Settings → Pages → Source选择GitHub Actions。
3. Actions → Daily watchlist and GitHub Pages → Run workflow。等待生成数据并部署成功。
4. 从Deploy shared snapshot步骤打开网站地址。以后每天北京时间07:40计划自动更新，时间可能延迟。

GitHub不解压ZIP；请先在电脑解压，再上传里面的文件和文件夹。

首次运行需要联网访问交易所目录、TradingView和历史行情源，可能因限流失败。成功后才有可分享网页；失败不表示已经发布。每轮最多尝试120只历史K线，其他候选仍在名单中，图表会随以后更新逐步补齐。以后扫描失败会保留此前网页。

这份初始包不包含此前桌面版的历史候选记录，记录从云端首次成功扫描开始积累。公开仓库的定时任务长期无活动时可能被停用，以Actions状态为准。
