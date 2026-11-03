"""群聊分析版 Jev 题目组（参考 jev-chat-jarvis/tools/jev 的措辞风格）。

口径：instructions/criteria 一律英文；state 里的聊天内容保留中文原文。
输出用固定中文标签渲染（build_state/format_card），没有自由文本生成环节。
"""

from __future__ import annotations

MAX_STATE_MESSAGES = 40

# 题目答案 key -> 中文展示
TOPIC_LABELS = {
    "casual_banter": "闲聊斗嘴",
    "practical_question": "实际求助/提问",
    "link_media_discussion": "讨论链接/视频/媒体",
    "debate_or_argument": "争论或吵架",
    "emotional_personal": "私人情绪话题",
    "other": "其他",
}
ACTION_LABELS = {
    "stay_silent": "保持安静别插话",
    "summarize_discussion": "总结当前讨论",
    "answer_open_question": "回答那个没人接的问题",
    "provide_relevant_info": "补充相关信息",
    "lighten_the_mood": "缓和气氛",
}
NEEDS_LABELS = {
    "information": "想要准确的信息/答案",
    "conflict_resolution": "需要有人调解冲突",
    "entertainment": "想要乐子/继续闲聊",
    "nothing": "什么都不缺，一切正常",
}

QUESTIONS: dict = {
    "group_atmosphere": {
        "type": "score",
        "instructions": (
            "Rate the overall atmosphere of this group chat from the recent messages. "
            "Match the current scene, not an earlier moment."
        ),
        "criteria": [
            "Quiet and friendly; low-energy small talk, nothing notable happening.",
            "Light banter with playful teasing; everyone sounds relaxed.",
            "Lively and energetic; many people talking fast about a shared interest.",
            "Loud chaos: many topics at once, spam or memes flooding, but still good-natured.",
            "Tension appears: someone sounds annoyed, short replies, mild sarcasm.",
            "Open conflict: members are blaming or attacking each other.",
        ],
    },
    "escalation_risk": {
        "type": "score",
        "instructions": (
            "How close is this group chat to a real fight or hurt feelings? "
            "Judge the present state. If a quarrel has already cooled down and people "
            "are joking again, score the cooled-down present. If someone dropped a serious "
            "accusation or ultimatum that has not been withdrawn, stay in the high bin "
            "even if others are joking around it."
        ),
        "criteria": [
            "No disagreement anywhere; pure casual chat or joking.",
            "A mild tease that is easy to laugh off; nothing pointed.",
            "A small complaint said without heat; others respond warmly or practically.",
            "Noticeable irritation; someone repeats a grievance or calls out a member.",
            "Sarcasm and short cold replies aimed at one person; a wrong reply would escalate.",
            "Openly upset: a member accuses another of not listening or disrespecting.",
            "Two or more members clearly arguing and blaming; bystanders start taking sides.",
            "Serious threat: 'report you', 'pull the group', 'block you', or similar, still standing.",
            "Insults and personal attacks are flying; the argument has taken over the chat.",
            "Active rupture: someone left the group, told everyone to shut up, or is exploding.",
        ],
    },
    "dominant_topic": {
        "type": "choice",
        "instructions": (
            "What is the dominant topic of the recent messages? Pick what most of the "
            "active speakers are engaged with, not a one-off side remark."
        ),
        "criteria": {
            "casual_banter": (
                "Light talk, memes, teasing, daily-life sharing with no ask and no conflict."
            ),
            "practical_question": (
                "Someone asked a concrete how-to / what-is / recommendation question and "
                "members are trying to answer or are waiting for an answer."
            ),
            "link_media_discussion": (
                "The conversation revolves around a shared link, video, article, or screenshot "
                "and people are reacting to that content."
            ),
            "debate_or_argument": (
                "Members disagree on a topic or with each other and are defending positions; "
                "temperature is above casual banter."
            ),
            "emotional_personal": (
                "Someone is sharing personal troubles, feelings, or a sensitive life event "
                "and the talk is about support, not information."
            ),
            "other": "None of the above fits the main thread.",
        },
    },
    "open_question": {
        "type": "noul",
        "instructions": (
            "Has anyone asked a concrete question in the recent messages that no one has "
            "answered yet? A rhetorical or rhetorical-style complaint does not count."
        ),
        "criteria": {
            "true": (
                "A clear, answerable question (how to do X, what is Y, does anyone have Z) "
                "was asked and no later message answers it."
            ),
            "false": (
                "All questions got answers or reactions, the question was rhetorical, "
                "or the asker moved on."
            ),
        },
    },
    "should_bot_speak": {
        "type": "noul",
        "instructions": (
            "Would it be appropriate for an AI assistant bot to interject in this group "
            "chat right now with an analysis or help? Consider the mood, not usefulness alone."
        ),
        "criteria": {
            "true": (
                "The chat is neutral or positive, someone needs information, or the bot has "
                "something clearly relevant; interjecting would not feel intrusive."
            ),
            "false": (
                "The chat is a private/emotional exchange, mid-conflict, or nothing the bot "
                "could add; a bot interjection would be noise or would pour oil on fire."
            ),
        },
    },
    "best_bot_action": {
        "type": "choice",
        "instructions": (
            "If the assistant bot were to speak, what type of action fits best? "
            "This is about the action type only, not timing. "
            "If the mood is tense or the chat does not need the bot, prefer stay_silent."
        ),
        "criteria": {
            "stay_silent": (
                "Nothing the bot could add right now; speaking would be noise, intrusive, "
                "or risky for the mood."
            ),
            "summarize_discussion": (
                "The thread is long or scattered and a compact summary would help members "
                "catch up."
            ),
            "answer_open_question": (
                "There is an unanswered concrete question the bot can plausibly answer "
                "better than waiting for members."
            ),
            "provide_relevant_info": (
                "Add a relevant fact, clarification, or context about the topic being discussed "
                "(without doing it for a person in need of support)."
            ),
            "lighten_the_mood": (
                "A light, harmless joke or warm remark would defuse mild tension in an "
                "otherwise friendly chat; never for real conflicts."
            ),
        },
    },
    "wind_down": {
        "type": "noul",
        "instructions": (
            "Has the conversation naturally wound down, with the last messages being "
            "closings, reactions, or silence rather than new questions or topics?"
        ),
        "criteria": {
            "true": (
                "Recent messages are reactions ('haha', 'ok', emoji-style short replies), "
                "goodbyes, or the same people repeating themselves; nothing is pending."
            ),
            "false": (
                "New topics, questions, or back-and-forth exchanges are still actively "
                "happening."
            ),
        },
    },
    "group_needs": {
        "type": "choice",
        "instructions": (
            "What does the group need most right now from the conversation's perspective? "
            "If everything is fine and the chat is just flowing, choose nothing."
        ),
        "criteria": {
            "information": (
                "An accurate answer, fact, or recommendation is the missing piece right now."
            ),
            "conflict_resolution": (
                "The argument needs de-escalation or a neutral party more than anything else."
            ),
            "entertainment": (
                "The chat wants fun: more memes, jokes, or playful content to keep going."
            ),
            "nothing": (
                "The conversation is fine on its own; members need nothing from anyone."
            ),
        },
    },
}


# ── 单条消息分析（/jev 无参数）──────────────────────────
# 卡片只展示：危险评级 / 真实意图（把握）/ 应对标签 / 候选回复（Jev 排序）
INTENT_LABELS = {
    "casual_chat": "随口闲聊",
    "seeking_help": "求助/要你办事",
    "venting_mocking": "发泄情绪/阴阳怪气",
    "testing_sarcasm": "试探讽刺",
    "provoking_conflict": "挑事引战",
    "sharing_info": "分享信息",
}

# 应对标签（对应原版"要具体行动 · 翻聊天记录 · 先别给实质"）
ADVICE_TAGS = {
    "needs_action": "要具体行动",
    "check_history": "翻聊天记录",
    "hold_substance": "先别给实质",
    "no_reply": "可以不回",
}

# 单条消息分析的题目组：分析最新一条消息本身，参考原仓库一对一场景的题目措辞
SINGLE_QUESTIONS: dict = {
    "conflict_risk": {
        "type": "score",
        "instructions": (
            "If left unanswered, how likely is this message to spark a conflict in the "
            "group? Judge the message itself in this context, not the current mood alone."
        ),
        "criteria": [
            "Pure content or small talk; nobody could take offense.",
            "A mild tease aimed at no one in particular.",
            "A pointed remark someone might personally feel, but most would laugh off.",
            "Sarcasm aimed at a member; they may reply in kind.",
            "Clear mockery or an accusation of a member; friends may step in.",
            "Open insult or challenge; the target is likely to fire back.",
            "Dragging in past grievances or piling on; several members may join the fight.",
            "Group-wide provocation (insulting the group, baiting everyone).",
            "Direct threats or doxxing-style attacks; admins should act.",
            "Already an active fight starter; quote wars are starting.",
        ],
    },
    "true_intent": {
        "type": "choice",
        "instructions": (
            "What is the sender's true intent with this message, given the surrounding "
            "conversation? Prefer tone and context over surface wording."
        ),
        "criteria": {
            "casual_chat": (
                "Light talk, memes, teasing with a laugh, or daily-life sharing; no ask, "
                "no conflict."
            ),
            "seeking_help": (
                "They want something done or answered: how to do X, what is Y, a file, "
                "a favor, a promise to be kept."
            ),
            "venting_mocking": (
                "They are blowing off frustration or taking a jab at someone; making someone "
                "feel it matters more than the literal content."
            ),
            "testing_sarcasm": (
                "They are checking whether someone notices, remembers, or dares to respond; "
                "surface wording is bait."
            ),
            "provoking_conflict": (
                "They want a fight: insulting, calling out, dragging past grievances, or "
                "daring others to argue."
            ),
            "sharing_info": (
                "They are contributing useful content: a link, news, an answer, or "
                "first-hand experience."
            ),
        },
    },
    "needs_action": {
        "type": "noul",
        "instructions": (
            "Does properly handling this message require a concrete action (sending a "
            "file, doing a task, keeping a promise), rather than just words?"
        ),
        "criteria": {
            "true": (
                "A concrete action or deliverable is being asked for; words alone "
                "will not resolve it."
            ),
            "false": (
                "A normal conversational reply is enough; nothing concrete is required."
            ),
        },
    },
    "check_history": {
        "type": "noul",
        "instructions": (
            "To judge or answer this message well, is it necessary to check earlier chat "
            "history or past commitments?"
        ),
        "criteria": {
            "true": (
                "The message references something said or promised earlier; history must "
                "be checked to respond correctly."
            ),
            "false": (
                "The message is self-contained; the recent context already shown is enough."
            ),
        },
    },
    "hold_substance": {
        "type": "noul",
        "instructions": (
            "If replying, should the reply deliberately hold back real substance or firm "
            "commitment for now?"
        ),
        "criteria": {
            "true": (
                "Better to stall or stay noncommittal: the request is unclear, suspicious, "
                "or jumping the gun; do not promise anything yet."
            ),
            "false": (
                "It is safe to reply directly and concretely; no reason to stall."
            ),
        },
    },
    "needs_reply": {
        "type": "noul",
        "instructions": (
            "Does this message need someone in the group to respond to it? "
            "Pure statements, reactions, and content shares usually do not."
        ),
        "criteria": {
            "true": (
                "There is a question, request, or clear bait that will look ignored "
                "if nobody answers."
            ),
            "false": (
                "Nothing is asked or baited; staying silent is natural and safe."
            ),
        },
    },
}


def extract_answers(result: dict) -> dict:
    answers: dict = result.get("answers") or {}
    if not answers:
        answers = {k: v for k, v in result.items() if isinstance(v, dict) and "type" in v}
    if not answers:
        raise RuntimeError("Jev 响应里没有题目答案")
    return answers


RISK_BANDS = ["无风险", "无伤大雅", "有人可能在意", "可能被顶回来", "朋友会介入",
              "对方会反击", "多人卷入", "全群挑衅", "管理员该管", "已经开始骂战"]
# 群聊整体冲突档位（escalation_risk 的 criteria 措辞与单条不同）
GROUP_RISK_BANDS = ["毫无争端", "无伤大雅", "略有微词", "有人不爽", "阴阳怪气",
                    "公开指责", "互相指责", "放狠话", "人身攻击", "关系破裂"]


def _risk_head(answer: dict, bands: list[str] = RISK_BANDS, label: str = "冲突") -> str:
    """危险评级行：🟢/🟡/🔴 + 分数 + 档位，低分段读起来不吓人。"""
    score = float((answer or {}).get("score", 0))
    band = bands[min(round(score), len(bands) - 1)]
    dot = "🟢" if score < 3 else "🟡" if score < 6 else "🔴"
    return f"{dot} {label} {score:.0f}/9 · {band}"


def _grasp(answer: dict, key: str) -> str:
    """所选选项的把握（概率优先，置信度兜底），无数据返回空串。"""
    a = answer or {}
    p = (a.get("probabilities") or {}).get(key)
    if not isinstance(p, (int, float)):
        p = a.get("confidence")
    return f"（把握 {p:.0%}）" if isinstance(p, (int, float)) else ""


def build_rank_questions(candidates: list[str]) -> dict:
    """把 LLM 生成的候选回复包成 Jev 排序题（动态 criteria，c1..cn）。"""
    return {
        "best_reply": {
            "type": "choice",
            "instructions": (
                "These are candidate replies to the latest message. Which one is the "
                "best to send next? Judge fit with the conversation, tone match, and "
                "risk of making things worse. Never invent work."
            ),
            "criteria": {f"c{i}": c for i, c in enumerate(candidates, 1)},
        }
    }


def rank_candidates(result: dict, candidates: list[str]) -> list[tuple[str, float]]:
    """从 Jev 排序响应里取出候选回复及其概率，按概率降序。"""
    ans = extract_answers(result).get("best_reply") or {}
    probs = ans.get("probabilities") or {}
    scored = [
        (c, float(probs.get(f"c{i}", 0.0))) for i, c in enumerate(candidates, 1)
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def format_single_card(
    result: dict, who: str, text: str, n_ctx: int,
    replies: list[tuple[str, float]] | None = None,
) -> str:
    """渲染单条消息分析卡片：评级/意图/应对 + 候选回复（Jev 排序）。"""
    a = extract_answers(result)

    intent = a.get("true_intent") or {}
    intent_key = intent.get("choice", "")
    intent_s = INTENT_LABELS.get(intent_key, "未知") + _grasp(intent, intent_key)

    tags = []
    if (a.get("needs_action") or {}).get("noul", 0) >= 0.5:
        tags.append(ADVICE_TAGS["needs_action"])
    if (a.get("check_history") or {}).get("noul", 0) >= 0.5:
        tags.append(ADVICE_TAGS["check_history"])
    if (a.get("hold_substance") or {}).get("noul", 0) >= 0.5:
        tags.append(ADVICE_TAGS["hold_substance"])
    if (a.get("needs_reply") or {}).get("noul", 0) < 0.5:
        tags.append(ADVICE_TAGS["no_reply"])
    advice_s = " · ".join(tags) if tags else "正常接话就行"

    src = (text or "").strip() or "（无文字）"
    lines = [
        f"🔍 单条消息分析 · 来自 {who or '群成员'}",
        f"原话：{src[:40]}",
        _risk_head(a.get("conflict_risk"), label="危险"),
        f"🎯 真实意图：{intent_s}",
        f"🧭 应对：{advice_s}",
    ]

    if replies:
        lines += ["💬 候选回复（Jev 排序）"]
        for i, (cand, p) in enumerate(replies, 1):
            lines.append(f"{i}. {cand}（{p:.0%}）")

    lines.append(
        f"（结合最近 {n_ctx} 条上下文判断）" if n_ctx > 0 else "（缓冲里没有更早的上下文）"
    )
    return "\n".join(lines)


def build_state(lines: list[str], group_id: int, max_messages: int = 20) -> dict:
    """把群聊缓冲行（"昵称：内容"）转成 Jev 的 state。

    聊天原文保留中文；昵称放 from（群聊场景没有固定的 her/me 双方）。
    """
    cap = min(max_messages, MAX_STATE_MESSAGES)
    msgs = []
    for line in lines[-cap:]:
        who, sep, text = line.partition("：")
        if not sep:
            who, text = "member", line
        msgs.append({"from": who or "member", "text": text})
    latest = msgs[-1]["from"] if msgs else ""
    return {
        "chat": {
            "platform": "qq_group",
            "group_id": str(group_id),
            "messages": msgs,
            "latest_from": latest,
        }
    }


def _fmt_score(name: str, answer: dict, labels: list[str]) -> str:
    score = float(answer.get("score", 0))
    idx = min(round(score), len(labels) - 1)
    conf = answer.get("confidence")
    conf_s = f" · 置信 {conf:.2f}" if isinstance(conf, (int, float)) else ""
    return f"{labels[idx]}（{score:.1f}/{len(labels) - 1}{conf_s}）"


def format_card(result: dict, n_lines: int) -> str:
    """把 Jev 响应渲染成固定模板的中文卡片（无自由文本），头行给重点。"""
    answers = extract_answers(result)

    topic = TOPIC_LABELS.get(
        (answers.get("dominant_topic") or {}).get("choice"), "未知"
    )
    open_q = "有" if (answers.get("open_question") or {}).get("noul", 0) >= 0.5 else "无"
    speak = (answers.get("should_bot_speak") or {}).get("noul", 0) >= 0.5
    action = ACTION_LABELS.get((answers.get("best_bot_action") or {}).get("choice"), "未知")
    needs = NEEDS_LABELS.get((answers.get("group_needs") or {}).get("choice"), "未知")
    wind = (answers.get("wind_down") or {})
    wind_s = "接近尾声" if wind.get("noul", 0) >= 0.5 else "还在进行"

    atmo = _fmt_score(
        "atmosphere", answers.get("group_atmosphere", {}),
        ["安静平和", "轻松闲聊", "热闹活跃", "沸腾吵闹", "出现火药味", "公开冲突"],
    )
    atmo_s = atmo.split("（", 1)[0]  # 去掉分数尾巴，气氛只需档位

    lines = [
        f"📊 群聊分析（最近 {n_lines} 条）",
        f"{_risk_head(answers.get('escalation_risk', {}), GROUP_RISK_BANDS)}",
        f"🎨 气氛：{atmo_s}",
        f"🎯 主题：{topic}",
        f"🧺 群需要：{needs}",
        f"❓ 没人答的提问：{open_q}",
        f"⏳ 对话：{wind_s}"
    ]
    if speak:
        lines.append(f"💡 若插话：{action}")
    else:
        lines.append("💡 此刻插话：不适合，先观望")
    return "\n".join(lines)
