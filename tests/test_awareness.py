"""觉察模式的结构、来源、预算及无工具边界；不调用外部模型。"""

import copy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import awareness
import main as runtime
from runtime_guards import RunBudget, get_current_budget

ORIGINAL = "朋友没有回复，我很失落，觉得对方不在乎我。"


def organization():
    return {
        "mode": "organize",
        "reflection": "朋友没有回复让你很失落，你也在想这是否意味着他不在乎你。",
        "response_type": "respond",
        "intent": {"kind": "none", "quote": None},
        "experiences": [{"quote": "朋友没有回复", "text": "你说朋友没有回复。", "source": "user_report"}],
        "feelings": [{"quote": "我很失落", "text": "失落。", "source": "user_explicit"}],
        "interpretations": [{"quote": "对方不在乎我", "text": "这是对对方态度的解释。", "source": "user_interpretation"}],
        "question": None,
    }


def safety_result():
    return {"mode": "safety_support", "risk": {"quote": "朋友没有回复", "text": "风险自述占位，仅测试协议。", "source": "user_report"}, "support": "urgent_help"}


def dialogue(response_type="respond", *, kind="none", quote=None, question=None):
    result = organization()
    result.update(response_type=response_type, intent={"kind": kind, "quote": quote}, question=question)
    result["reflection"] = "我会按你这次的表达来回应。"
    for section in awareness.SECTION_SOURCES:
        result[section] = []
    return result


def completion(content, *, finish="stop", usage=True, **fields):
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    message = SimpleNamespace(content=content, tool_calls=None, function_call=None, refusal=None)
    for name, value in fields.items():
        setattr(message, name, value)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30) if usage else None,
    )


def client_for(*responses):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=responses))),
        close=Mock(),
    )


class ValidationTests(unittest.TestCase):
    def validate(self, result, original=ORIGINAL):
        return awareness.validate_result(json.dumps(result, ensure_ascii=False), original)

    def test_valid_sections_and_sources(self):
        result = organization()
        self.assertEqual(self.validate(result), result)
        result["feelings"][0]["source"] = "model_inference"
        self.assertEqual(self.validate(result)["feelings"][0]["source"], "model_inference")
        for section in awareness.SECTION_SOURCES:
            result[section] = []
        self.assertEqual(self.validate(result), result)
        result["question"] = "你愿意补充事情发生的时间吗？可以跳过。"
        result["response_type"] = "clarify"
        self.assertEqual(self.validate(result), result)

    def test_default_output_is_brief_without_changing_provenance(self):
        result = organization()
        before = copy.deepcopy(result)
        rendered = awareness.render_result(result)
        self.assertEqual(rendered, result["reflection"])
        self.assertNotIn("发生了什么", rendered)
        self.assertNotIn("原文：", rendered)
        self.assertNotIn("用户自述，未独立核实", rendered)
        self.assertEqual(result, before)
        self.assertEqual(self.validate(result), before)

    def test_natural_output_keeps_visible_escapes_and_tentative_wording(self):
        result = organization()
        result["feelings"][0].update(text="也许是失落\n你可以否认。", source="model_inference")
        result["question"] = "要补充吗？\n可以跳过。"
        result["reflection"] = "也许是失落\n你可以否认。"
        result["response_type"] = "clarify"
        rendered = awareness.render_result(self.validate(result))
        self.assertIn("也许是失落\\n你可以否认。", rendered)
        self.assertEqual(result["feelings"][0]["source"], "model_inference")
        self.assertIn("要补充吗？\\n可以跳过。", rendered)

    def test_structure_and_provenance_errors(self):
        cases = []
        result = organization()
        result["confidence"] = 0.99
        cases.append(result)
        result = organization()
        result["feelings"] = None
        cases.append(result)
        result = organization()
        result["question"] = ["问题一", "问题二"]
        cases.append(result)
        for field, value in (("quote", "并不存在的原文"), ("quote", ""), ("text", 1), ("source", "verified"), ("extra", True)):
            result = organization()
            result["experiences"][0][field] = value
            cases.append(result)
        for result in cases:
            with self.subTest(result=result), self.assertRaises(awareness.ValidationError):
                self.validate(result)

    def test_multi_turn_quotes_cannot_use_assistant_text_or_join_turns(self):
        originals = ("朋友没有回复", "不是失落，是生气。")
        result = organization()
        result["interpretations"] = []
        result["feelings"] = [{"quote": "生气", "text": "生气。", "source": "user_explicit"}]
        self.assertEqual(awareness.validate_result(json.dumps(result), originals), result)
        for quote in ("朋友没有回复\n不是失落", "助手猜测的难过"):
            result["experiences"][0]["quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(awareness.ValidationError):
                awareness.validate_result(json.dumps(result), originals)

    def test_limits_do_not_silently_truncate(self):
        result = organization()
        result["feelings"] *= awareness.MAX_SECTION_ITEMS + 1
        with self.assertRaises(awareness.ValidationError):
            self.validate(result)
        result = organization()
        result["question"] = "问" * (awareness.MAX_QUESTION_CHARS + 1)
        with self.assertRaises(awareness.ValidationError):
            self.validate(result)
        with self.assertRaises(awareness.AwarenessError) as raised:
            awareness.validate_result(" " * (awareness.MAX_REPLY_CHARS + 1), ORIGINAL)
        self.assertEqual(raised.exception.code, "reply_limit")

    def test_safety_branch_is_exclusive_and_not_repaired(self):
        result = safety_result()
        self.assertEqual(self.validate(result), result)
        self.assertIn(awareness.SAFETY_SUPPORT, awareness.render_result(result))
        for field, value in (("support", "other"), ("question", None), ("risk", None)):
            invalid = copy.deepcopy(result)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaises(awareness.AwarenessError) as raised:
                self.validate(invalid)
            self.assertEqual(raised.exception.code, "safety_validation")

    def test_normal_joiners_are_preserved(self):
        original = "今天和家人👨‍👩‍👧‍👦一起吃饭。"
        awareness.validate_input(original)
        result = organization()
        result["experiences"][0]["quote"] = original
        result["experiences"][0]["text"] = "一家人👨‍👩‍👧‍👦一起吃饭。"
        result["feelings"] = result["interpretations"] = []
        for ascii_output in (True, False):
            raw = json.dumps(result, ensure_ascii=ascii_output)
            with self.subTest(ascii_output=ascii_output):
                self.assertEqual(awareness.validate_result(raw, original), result)
        awareness.validate_input("正常连接符" + chr(0x200C) + "保留")

    def test_dangerous_characters_and_credentials_are_rejected(self):
        for char in (chr(0x1B), chr(0), chr(0x202E), chr(0x2066), chr(0xD800)):
            with self.subTest(codepoint=ord(char)), self.assertRaises(awareness.AwarenessError):
                awareness.validate_input("输入" + char)
        with self.assertRaises(awareness.AwarenessError) as raised:
            awareness.validate_input("password=synthetic-only-value")
        self.assertEqual(raised.exception.code, "credentials")
        result = organization()
        result["feelings"][0]["text"] = "文字" + chr(0x1B) + "[31m"
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.validate(result)
        self.assertEqual(raised.exception.code, "reply_unsafe")

    def test_empty_or_oversized_input_and_json_are_rejected(self):
        for text in (None, "", "  ", "字" * (awareness.MAX_INPUT_CHARS + 1)):
            with self.subTest(kind=type(text).__name__), self.assertRaises(awareness.AwarenessError):
                awareness.validate_input(text)
        for raw in ("not json", "[]", "null", '{"mode":"organize","mode":"organize"}', '{"mode":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(awareness.ValidationError):
                awareness.validate_result(raw, ORIGINAL)


class ConsentTests(unittest.TestCase):
    def validate(self, result, text=ORIGINAL, state="off"):
        return awareness.validate_result(json.dumps(result, ensure_ascii=False), text, guidance_state=state)

    def test_invitation_and_direct_consent_enable_current_topic_only(self):
        invitation = dialogue("invite", question="你愿意一起看看此刻的感受吗？可以跳过。")
        self.validate(invitation)
        self.assertEqual(awareness.guidance_after_reply(invitation, "off"), "pending")
        result = dialogue("guide", kind="consent", quote="我愿意。", question="此刻最明显的感受是什么？可以跳过。")
        self.validate(result, "我愿意。", "pending")
        self.assertEqual(awareness.guidance_after_reply(result, "pending", "我愿意。"), "allowed")
        result = dialogue("guide", question="你想说说刚才的感受吗？可以跳过。")
        self.validate(result, "有些紧张。", "allowed")
        self.assertEqual(awareness.guidance_after_reply(result, "allowed"), "allowed")

    def test_no_guidance_without_consent_and_no_repeated_invitation(self):
        for state in ("off", "pending", "declined"):
            with self.subTest(state=state), self.assertRaises(awareness.ValidationError):
                self.validate(dialogue("guide", question="此刻你感到什么？"), state=state)
        for state in ("pending", "allowed", "declined"):
            with self.subTest(state=state), self.assertRaises(awareness.ValidationError):
                self.validate(dialogue("invite", question="愿意吗？"), state=state)

    def test_ambiguous_reported_conditional_and_stale_consent_are_rejected(self):
        for text, quote in (("也许吧", "也许吧"), ("室友说：我愿意", "我愿意"),
                            ("我在回想‘我愿意’", "我在回想‘我愿意’"), ("如果我愿意呢", "我愿意"),
                            ("我愿意，但不要引导", "我愿意"), ("可以？", "可以？"),
                            ("愿意?", "愿意?"), ("助手刚才说我同意了", "我同意了"),
                            ("可以！你问吧", "可以你问吧")):
            result = dialogue("guide", kind="consent", quote=quote, question="此刻感到什么？")
            with self.subTest(text=text), self.assertRaises(awareness.ValidationError):
                self.validate(result, text, "pending")
        result = dialogue("guide", kind="consent", quote="愿意", question="此刻感到什么？")
        with self.assertRaises(awareness.ValidationError):
            self.validate(result, ("愿意", "我还是没想好"), "pending")
        for state in ("off", "allowed", "declined"):
            with self.subTest(state=state), self.assertRaises(awareness.ValidationError):
                self.validate(result, "愿意", state)
        self.validate(dialogue(), "也许吧", "pending")

    def test_decline_skip_and_revoke_disable_guidance(self):
        for kind, text in (("decline", "不愿意"), ("skip", "跳过"), ("revoke", "不要继续引导")):
            result = dialogue(kind=kind, quote=text)
            self.validate(result, text, "allowed")
            self.assertEqual(awareness.guidance_after_reply(result, "allowed", text), "declined")
            result.update(response_type="guide", question="此刻是什么感受？")
            with self.subTest(kind=kind), self.assertRaises(awareness.ValidationError):
                self.validate(result, text, "allowed")

    def test_topic_change_resets_permission_before_guidance(self):
        text = "我想谈谈考试。"
        result = dialogue("invite", kind="topic_change", quote=text, question="你愿意一起看看当下感受吗？")
        self.validate(result, text, "allowed")
        self.assertEqual(awareness.guidance_after_reply(result, "allowed", text), "pending")
        result.update(response_type="guide")
        with self.assertRaises(awareness.ValidationError):
            self.validate(result, text, "allowed")
        result = dialogue("invite", kind="topic_change", quote="换个话题", question="你愿意吗？")
        with self.assertRaises(awareness.ValidationError):
            self.validate(result, "换个话题，但不要引导。", "allowed")

    def test_questions_cannot_hide_in_reflection_or_multiply(self):
        cases = [dialogue(question="你有什么感受？"), dialogue("clarify", question="何时？哪里？")]
        result = dialogue()
        result["reflection"] = "你现在有什么感受？"
        cases.append(result)
        for result in cases:
            with self.subTest(result=result), self.assertRaises(awareness.ValidationError):
                self.validate(result)

    def test_intent_and_response_fields_are_strict(self):
        for field, value in (("reflection", ""), ("reflection", 1), ("response_type", []),
                             ("intent", {"kind": "none", "quote": "引用"}),
                             ("intent", {"kind": "unknown", "quote": None}),
                             ("intent", {"kind": "none", "quote": None, "extra": True})):
            result = dialogue()
            result[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(awareness.ValidationError):
                self.validate(result)

    def test_end_is_exclusive_and_safety_resets_consent(self):
        result = dialogue("end", kind="end", quote="今天就聊到这里")
        self.validate(result, "今天就聊到这里", "allowed")
        self.assertEqual(awareness.guidance_after_reply(result, "allowed"), "off")
        result["response_type"] = "respond"
        with self.assertRaises(awareness.ValidationError):
            self.validate(result, "今天就聊到这里", "allowed")
        self.assertEqual(awareness.guidance_after_reply(safety_result(), "allowed"), "off")

    def test_direct_revocation_survives_request_failure(self):
        self.assertEqual(awareness.input_guidance_state("我不希望你再引导了", "allowed"), "declined")
        self.assertEqual(awareness.input_guidance_state("换个话题，但不要再问", "allowed"), "declined")

    def test_leaving_old_topic_does_not_decline_new_topic(self):
        text = "我不想继续聊这件事，换个话题，我担心考试。"
        self.assertEqual(awareness.input_guidance_state(text, "allowed"), "off")
        result = dialogue("invite", kind="topic_change", quote="换个话题", question="愿意一起看看考试带来的感受吗？")
        self.validate(result, text, "allowed")
        self.assertEqual(awareness.guidance_after_reply(result, "off", text), "pending")
        self.assertEqual(awareness.input_guidance_state("跳过这个问题，换个话题", "allowed"), "off")
        self.assertEqual(awareness.input_guidance_state("跳过这个问题，换个话题，但不要引导", "allowed"), "declined")


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        self.capture = redirect_stderr(self.stderr)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def organize(self, client, *, budget=None):
        return awareness.organize(client, "offline-model", ORIGINAL, budget=budget or RunBudget(environ={}))

    def test_new_path_omits_tools_and_accounts_only_its_messages(self):
        client = client_for(completion(organization()))
        budget = RunBudget(environ={})
        budget.before_model_request = Mock(wraps=budget.before_model_request)
        self.assertEqual(self.organize(client, budget=budget), organization())
        request = client.chat.completions.create.call_args.kwargs
        self.assertTrue({"tools", "functions", "tool_choice", "function_call"}.isdisjoint(request))
        self.assertEqual(request["messages"], budget.before_model_request.call_args.args[0])
        self.assertEqual(budget.snapshot()["model_requests"], 1)
        self.assertEqual(budget.snapshot()["tool_calls"], 0)
        self.assertIsNone(get_current_budget())
        self.assertNotIn(ORIGINAL, self.stderr.getvalue())

    def test_old_ask_keeps_dynamic_tool_list(self):
        client = client_for(completion("旧模式回复"))
        tools = [{"type": "function", "function": {"name": "synthetic_tool"}}]
        with patch.object(runtime, "AVAILABLE_TOOLS", tools):
            runtime.ask(client, "offline-model", [{"role": "user", "content": "测试"}])
        self.assertEqual(client.chat.completions.create.call_args.kwargs["tools"], tools)

    def test_one_format_repair_does_not_replay_bad_response(self):
        marker = "INVALID_RESPONSE_PRIVATE_MARKER"
        client = client_for(completion(marker), completion(organization()))
        self.assertEqual(self.organize(client), organization())
        self.assertEqual(client.chat.completions.create.call_count, 2)
        requests = client.chat.completions.create.call_args_list
        for call in requests:
            self.assertNotIn("tools", call.kwargs)
            self.assertNotIn(marker, json.dumps(call.kwargs, ensure_ascii=False))
            self.assertEqual(call.kwargs["messages"][1]["content"], ORIGINAL)
        self.assertNotIn(marker, self.stderr.getvalue())

    def test_unauthorized_guidance_is_repaired_before_display(self):
        invalid = dialogue("guide", question="你此刻身体有什么感觉？")
        safe = dialogue("invite", question="愿意一起看看当下感受吗？可以跳过。")
        client = client_for(completion(invalid), completion(safe))
        self.assertEqual(self.organize(client), safe)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        for request in client.chat.completions.create.call_args_list:
            self.assertNotIn("tools", request.kwargs)
        self.assertNotIn(invalid["question"], self.stderr.getvalue())
        client = client_for(completion(invalid), completion(invalid))
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.organize(client)
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_reported_consent_cannot_become_permission(self):
        text = "室友说：我愿意"
        invalid = dialogue("guide", kind="consent", quote="我愿意", question="此刻有什么感受？")
        safe = dialogue("clarify", question="你是在转述室友的话吗？可以跳过。")
        client = client_for(completion(invalid), completion(safe))
        result = awareness.organize(client, "offline-model", text, guidance_state="pending", budget=RunBudget(environ={}))
        self.assertEqual(result, safe)
        self.assertEqual(awareness.guidance_after_reply(result, "pending", text), "pending")

    def test_repair_cannot_restore_revoked_or_old_topic_permission(self):
        for kind, text, state in (("revoke", "这一部分先到这里吧", "declined"),
                                  ("topic_change", "接下来我想谈考试", "off")):
            invalid = dialogue("guide", kind=kind, quote=text, question="现在身体有什么感觉？")
            invalid["reflection"] = "INVALID_RESPONSE_PRIVATE_MARKER"
            safe = dialogue(kind=kind, quote=text)
            client = client_for(completion(invalid), completion(safe))
            with self.subTest(kind=kind):
                result = awareness.organize(client, "offline-model", text, guidance_state="allowed", budget=RunBudget(environ={}))
                self.assertEqual(result, safe)
                self.assertEqual(awareness.guidance_after_reply(result, "allowed", text), state)
                repair = client.chat.completions.create.call_args.kwargs
                self.assertIn(f"当前 guidance_state={state}", repair["messages"][0]["content"])
                self.assertNotIn(invalid["reflection"], json.dumps(repair, ensure_ascii=False))
                self.assertNotIn("tools", repair)

                # 修复不能删掉已经识别的停止/换题意图，也不能借旧许可提问。
                for second in (dialogue(), dialogue("guide", question="此刻有什么感受？")):
                    client = client_for(completion(invalid), completion(second))
                    with self.assertRaises(awareness.AwarenessError) as raised:
                        awareness.organize(client, "offline-model", text, guidance_state="allowed", budget=RunBudget(environ={}))
                    self.assertEqual(raised.exception.code, "validation")
                    self.assertEqual(raised.exception.guidance_state, state)
                    self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_assistant_intent_and_old_user_quote_cannot_revoke_during_repair(self):
        text = "我还有一些话想说"
        history = [{"role": "user", "content": "我之前不想引导"},
                   {"role": "assistant", "content": "你可以说不要引导"}]
        for quote in ("我之前不想引导", "不要引导"):
            invalid = dialogue(kind="revoke", quote=quote)
            client = client_for(completion(invalid), completion(dialogue()))
            result = awareness.organize(client, "offline-model", text, history=history, guidance_state="allowed", budget=RunBudget(environ={}))
            self.assertEqual(result, dialogue())
            self.assertIn("当前 guidance_state=allowed", client.chat.completions.create.call_args.kwargs["messages"][0]["content"])

    def test_repair_only_tightens_permission_even_if_intent_changes(self):
        for text, first_kind in (("不要继续引导，换个话题", "topic_change"),
                                 ("这一部分先到这里吧，接下来我想谈考试", "revoke")):
            first = dialogue("guide", kind=first_kind, quote=text, question="此刻有什么感受？")
            for second_kind in ("topic_change", "end"):
                second = dialogue("guide", kind=second_kind, quote=text, question="现在呢？")
                client = client_for(completion(first), completion(second))
                with self.subTest(text=text, second_kind=second_kind), self.assertRaises(awareness.AwarenessError) as raised:
                    awareness.organize(client, "offline-model", text, guidance_state="allowed", budget=RunBudget(environ={}))
                self.assertEqual(raised.exception.guidance_state, "declined")
                repair = client.chat.completions.create.call_args.kwargs
                self.assertIn("当前 guidance_state=declined", repair["messages"][0]["content"])
                self.assertIn(f"intent.kind 须保留 {first_kind}", repair["messages"][-1]["content"])

    def test_repair_context_budget_does_not_display_unauthorized_question(self):
        invalid = dialogue("guide", question="UNAUTHORIZED_QUESTION？")
        client = client_for(completion(invalid), completion(organization()))
        first_context = [{"role": "system", "content": awareness.SYSTEM_PROMPT + "\n当前 guidance_state=off。"},
                         {"role": "user", "content": ORIGINAL}]
        limit = len(json.dumps(first_context, ensure_ascii=False, separators=(",", ":")))
        with self.assertRaises(awareness.AwarenessError) as raised:
            awareness.organize(client, "offline-model", ORIGINAL, budget=RunBudget(environ={"MINI_AGENT_MAX_CONTEXT_CHARS": str(limit)}))
        self.assertEqual(raised.exception.code, "budget")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn(invalid["question"], self.stderr.getvalue())

    def test_second_invalid_reply_fails_without_third_request(self):
        client = client_for(completion("invalid"), completion("still invalid"))
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.organize(client)
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertIsNone(get_current_budget())

    def test_safety_decode_errors_never_downgrade_to_organization(self):
        original = safety_result()
        raw = json.dumps(original, ensure_ascii=False)
        cases = [raw.replace('"source": "user_report"', '"source": "user_report", "source": "user_report"'), raw.replace('"text": "风险自述占位，仅测试协议。"', '"text": NaN')]
        for raw in cases:
            client = client_for(completion(raw), completion(organization()))
            with self.subTest(raw=raw):
                with self.assertRaises(awareness.AwarenessError) as raised:
                    self.organize(client)
                self.assertEqual(raised.exception.code, "safety_validation")
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_deep_reply_is_not_retried_even_with_safety_marker(self):
        nesting = awareness.MAX_JSON_DEPTH + 1
        raw = '{"mode":"safety_support","extra":' + "[" * nesting + "0" + "]" * nesting + "}"
        client = client_for(completion(raw), completion(organization()))
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.organize(client)
        self.assertEqual(raised.exception.code, "reply_limit")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_malformed_safety_json_never_enters_format_repair(self):
        cases = (
            '{"mode":"safety_support","risk":',
            '{"risk": invalid, "mode":"safety_support"}',
            r'{"\u006dode":"safety_support","risk":',
        )
        for raw in cases:
            client = client_for(completion(raw), completion(organization()))
            with self.subTest(raw=raw):
                with self.assertRaises(awareness.AwarenessError) as raised:
                    self.organize(client)
                self.assertEqual(raised.exception.code, "safety_validation")
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_quoted_safety_marker_does_not_select_safety_mode(self):
        raw = json.dumps({"text": '"mode":"safety_support"'})
        client = client_for(completion(raw), completion(organization()))
        self.assertEqual(self.organize(client), organization())
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_non_format_failures_do_not_retry(self):
        call = SimpleNamespace(id="forbidden", function=SimpleNamespace(name="write_file", arguments="{}"))
        cases = [
            (completion(None), "empty"),
            (completion(" "), "empty"),
            (completion(organization(), finish="length"), "finish"),
            (completion(organization(), finish="content_filter"), "finish"),
            (completion(organization(), finish=None), "finish"),
            (completion(organization(), refusal="PRIVATE_PROVIDER_MARKER"), "refusal"),
            (completion(organization(), tool_calls=[call]), "tools"),
            (completion(organization(), function_call=SimpleNamespace(name="legacy", arguments="{}")), "tools"),
            (SimpleNamespace(choices=[]), "protocol"),
        ]
        for reply, code in cases:
            client = client_for(reply, completion(organization()))
            with self.subTest(code=code), self.assertRaises(awareness.AwarenessError) as raised:
                self.organize(client)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn("PRIVATE_PROVIDER_MARKER", self.stderr.getvalue())

    def test_usage_and_context_budget_can_prevent_requests(self):
        client = client_for(completion("invalid", usage=False), completion(organization()))
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.organize(client)
        self.assertEqual(raised.exception.code, "budget")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        client = client_for(completion(organization()))
        with self.assertRaises(awareness.AwarenessError) as raised:
            self.organize(client, budget=RunBudget(environ={"MINI_AGENT_MAX_CONTEXT_CHARS": "1"}))
        self.assertEqual(raised.exception.code, "budget")
        client.chat.completions.create.assert_not_called()

    def test_cancellation_restores_budget_context(self):
        client = client_for(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.organize(client)
        self.assertIsNone(get_current_budget())
        self.assertEqual(client.chat.completions.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
