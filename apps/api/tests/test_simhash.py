"""SimHash 的判别力测试。

重点不是「指纹算得对不对」（那没法断言），而是三条性质：
同一篇稿子改几个字距离要小、不同文章距离要大、文本太短拒绝出指纹。
"""

from __future__ import annotations

from app.utils.simhash import BITS, hamming, simhash, tokenize

ARTICLE = (
    "大模型推理成本在过去一年里下降了接近一个数量级。多家厂商通过量化、蒸馏与投机采样，"
    "把单位 token 的推理价格压到了原来的十分之一。这一变化直接改变了应用侧的产品设计："
    "过去需要精打细算的上下文长度，现在可以放开使用；过去只能用在小流量场景的模型，"
    "现在可以承接全量请求。与此同时，推理框架的调度策略成为新的竞争点，"
    "批处理大小、显存占用与首字延迟之间的权衡，决定了同样的硬件能承载多少并发。"
)

# 同一篇稿子被转载时的典型改动：换了标题、加了一句站点导语、结尾补了版权声明
REPOST = (
    "【转载】大模型推理成本在过去一年里下降了接近一个数量级。多家厂商通过量化、蒸馏与投机采样，"
    "把单位 token 的推理价格压到了原来的十分之一。这一变化直接改变了应用侧的产品设计："
    "过去需要精打细算的上下文长度，现在可以放开使用；过去只能用在小流量场景的模型，"
    "现在可以承接全量请求。与此同时，推理框架的调度策略成为新的竞争点，"
    "批处理大小、显存占用与首字延迟之间的权衡，决定了同样的硬件能承载多少并发。"
    "本文转载自某某科技，如有侵权请联系删除。"
)

UNRELATED = (
    "联赛第三轮的比赛在雨中进行。主队在上半场通过一次角球机会取得领先，"
    "客队下半场换人调整后连续制造威胁，第八十七分钟扳平比分。"
    "赛后主教练表示，场地积水影响了球队的传控节奏，但球员的拼抢态度值得肯定。"
    "下一轮球队将客场作战，主力中场因累积黄牌停赛。"
)

# 同一领域（推理基础设施）但另一位作者独立撰写：讲的是显存调度与集群利用率，
# 行文和用词与 ARTICLE 几乎没有重合。这正是「同一事件多源报道」的典型形态。
SAME_TOPIC_OTHER_WRITER = (
    "在集群层面，吞吐的瓶颈常常不在算力本身，而在显存带宽与调度粒度。"
    "当一批请求的长度差异很大时，按到达顺序组批会让长请求拖住整批的完成时间，"
    "于是出现了连续批处理与分页显存的方案，把键值缓存切成固定大小的块按需分配。"
    "工程上还要考虑抢占：高优先级请求到达时能否让低优先级任务让出显存槽位，"
    "以及让出之后如何恢复。这些取舍直接决定了单位成本与尾延迟的关系。"
)


def test_same_article_with_edits_is_close():
    a = simhash(ARTICLE)
    b = simhash(REPOST)
    assert a and b
    # 转载只加了前缀和尾注，实测距离 8，必须落在阈值 10 以内。
    # 教科书写 3，但那是英文语料的值；中文 bigram 下转载的真实距离就是 8。
    assert hamming(a, b) <= 10


def test_unrelated_articles_are_far():
    a = simhash(ARTICLE)
    b = simhash(UNRELATED)
    assert a and b
    # 实测 34~36。与转载的 8 之间有足够空档，阈值放在中间不会两头误判
    assert hamming(a, b) > 20


def test_same_topic_different_writing_is_far():
    # 同一领域但不同作者独立写的两篇稿子，必须离得远——这正是「同一事件多源报道」
    # 的情形，误判成转载会导致合并掉独立来源
    a = simhash(ARTICLE)
    b = simhash(SAME_TOPIC_OTHER_WRITER)
    assert a and b
    assert hamming(a, b) > 20


def test_short_text_has_no_fingerprint():
    # 少于 20 个 token 的「正文」多半是导航条或错误页，出指纹只会制造假重复
    assert simhash("加载中") is None
    assert simhash("") is None


def test_fingerprint_is_stable_across_calls():
    # 用 blake2b 而不是内置 hash()：内置 hash 对字符串加了进程级随机盐，
    # 重启后指纹全变，存进数据库就永久失效
    assert simhash(ARTICLE) == simhash(ARTICLE)


def test_tokenize_uses_cjk_bigrams():
    tokens = tokenize("人工智能")
    # 四个汉字应切成三个 bigram，而不是四个单字
    assert tokens == ["人工", "工智", "智能"]


def test_tokenize_drops_english_stopwords():
    tokens = tokenize("the model is a transformer for the task")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "model" in tokens
    assert "transformer" in tokens


def test_hamming_with_missing_side():
    # 一边没有指纹时视为完全不同，而不是抛异常——判重调用方不该为此写分支
    assert hamming(None, "0" * 16) == BITS
    assert hamming("0" * 16, None) == BITS
