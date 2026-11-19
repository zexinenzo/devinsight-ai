"""正文抽取的判定逻辑测试。

抽取算法本身是启发式的，断言「抽出完整的正文」不现实；这里只断言三条业务相关
的性质：认得语义标签、不会被导航挤掉、抽不到时明确返回 None。

最后一条最重要——返回 None 会让 content_status 记成 empty 而不是 failed，
两者对重试策略的含义完全相反。
"""

from __future__ import annotations

from app.utils.webpage import extract_main_text

ARTICLE_HTML = """
<html><head><title>标题</title><style>.x{color:red}</style></head>
<body>
<nav><a href="/">首页</a><a href="/hot">热门</a><a href="/about">关于</a></nav>
<header><h1>站点头部</h1></header>
<article>
  <h1>大模型推理成本下降</h1>
  <p>过去一年里，大模型推理的单位成本下降了接近一个数量级，主要来自量化与蒸馏技术的成熟。</p>
  <p>这一变化让应用侧的产品设计发生了明显转向：过去需要精打细算的上下文长度，现在可以放开使用。</p>
  <p>推理框架的调度策略成为新的竞争点，批处理大小与显存占用之间的权衡决定了同样的硬件能承载多少并发。</p>
</article>
<aside><p>相关阅读：某某公司的融资消息</p></aside>
<footer><p>版权所有 某某科技</p></footer>
<script>console.log("埋点")</script>
</body></html>
"""

DENSITY_HTML = """
<html><body>
<div class="nav"><p>首页</p><p>热门</p><p>关于我们</p></div>
<div class="wrapper"><div class="inner"><div class="body">
  <p>第一段正文讲的是推理成本的变化，涉及量化、蒸馏和投机采样这几种常见手段。</p>
  <p>第二段正文讲的是调度策略，批处理大小、显存占用与首字延迟之间存在明确的权衡关系。</p>
  <p>第三段正文讲的是应用侧影响，上下文长度放开之后，多轮对话与长文档处理的场景明显增多。</p>
</div></div></div>
<div class="sidebar"><p>广告位</p></div>
</body></html>
"""


def test_prefers_article_tag():
    text = extract_main_text(ARTICLE_HTML)
    assert text is not None
    assert "大模型推理成本下降" in text
    # 导航、侧栏、页脚的文字不该混进来
    assert "关于我们" not in text
    assert "相关阅读" not in text
    assert "版权所有" not in text


def test_falls_back_to_paragraph_density():
    text = extract_main_text(DENSITY_HTML)
    assert text is not None
    assert "第一段正文" in text
    assert "第三段正文" in text


def test_returns_none_for_short_page():
    # 登录墙、纯图片页：抓到了 HTML 但没有可用正文。
    # 返回 None 让调用方记 empty 而不是 failed——重试一百次也不会变
    assert extract_main_text("<html><body><p>请登录后查看</p></body></html>") is None
    assert extract_main_text("") is None


def test_strips_script_and_style():
    text = extract_main_text(ARTICLE_HTML)
    assert text is not None
    assert "埋点" not in text
    assert "color:red" not in text
