# -*- coding: utf-8 -*-
#json_checker.py — 全プロバイダ共通のJSONパーサ（契約は prompts/ が定義）
"""
3つの役割それぞれの出力を検証する。各役割＝各プロンプト＝各 handle 関数。

  handle_goal(text)      ← prompts/goal.txt      : type "goal"
  handle_task(text)      ← prompts/system.txt    : command / file / code / assist
  handle_assistant(text) ← prompts/assistant.txt : web_search / summary / assist

str でも 各プロバイダの Response でも受け取れる（.content を自動で取り出す）。
JSON契約は prompts/ が決めるものなので、どのLLMが出力したかには依存しない。
"""
import json

GOAL_TYPE = "goal"
ASSISTANT_TYPES = ("web_search", "summary", "assist")
TASK_TYPES = ("command", "file", "code", "assist", "web_search",
              "ssh_connect", "ssh_disconnect", "tool",
              "server_stop", "server_list")


def _to_text(value) -> str | None:
    """str でも Response でも受け取れるようにする。"""
    if value is None:
        return None
    content = getattr(value, "content", None)  # Response(dataclass) は content を持つ
    if content is not None:
        return content
    return str(value)


def _extract_json(text: str):
    """
    文章に混じったJSONから、最初にパースできるオブジェクトを1個だけ取り出す。
    ```json フェンスや前後の説明文、後ろに別のJSONが続くケースに加えて、
    本文より前の説明文や<think>ブロックに { が混じるケースにも耐える。
    """
    if not text:
        return None, "入力が空"

    start = text.find("{")
    if start == -1:
        return None, "JSONが見つからない"

    decoder = json.JSONDecoder()
    last_err = None
    while start != -1:
        try:
            # raw_decode は先頭のJSONを1個だけ読み、後ろの余計な文字は無視する
            data, _ = decoder.raw_decode(text[start:])
            return data, None
        except json.JSONDecodeError as e:
            last_err = e
            start = text.find("{", start + 1)  # 失敗したら次の { から再試行

    # 厳密パースが全滅した場合、LLMがやりがちな軽微な崩れを補正して再挑戦する。
    # （末尾カンマ、全角引用符、Python風 True/False/None、シングルクォート等）
    fixed = _lenient_json_fix(text)
    if fixed is not None:
        return fixed, None

    return None, f"JSONが壊れている: {last_err}"


def _lenient_json_fix(text: str):
    """LLM出力にありがちなJSONの軽微な崩れを補正してパースを試みる。
    成功すればdict、無理ならNone。"""
    import re
    s = text.find("{")
    e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    frag = text[s:e + 1]
    # コードフェンス除去
    frag = frag.replace("```json", "").replace("```", "")
    # 全角引用符→半角
    frag = frag.replace("“", '"').replace("”", '"').replace("’", "'")
    # Python風リテラルをJSONへ
    frag = re.sub(r"\bTrue\b", "true", frag)
    frag = re.sub(r"\bFalse\b", "false", frag)
    frag = re.sub(r"\bNone\b", "null", frag)
    # 末尾カンマ除去（ } や ] の直前のカンマ）
    frag = re.sub(r",\s*([}\]])", r"\1", frag)
    candidates = [frag]
    # シングルクォートのキー/値をダブルクォートに（最後の手段）
    if "'" in frag and '"' not in frag:
        candidates.append(frag.replace("'", '"'))
    for c in candidates:
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def _normalize_tags(tags) -> list[str]:
    """索引のブレを防ぐため tags を 小文字・strip・空除去・重複除去 する。"""
    if not isinstance(tags, list):
        return []
    seen, out = set(), []
    for t in tags:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ======================================================================== #
# goal（goal.txt）: 最終ゴール ＋ 目的リスト ＋ 索引タグ
# ======================================================================== #
def parse_goal_output(text) -> tuple[dict | None, str | None]:
    data, err = _extract_json(_to_text(text))
    if err:
        return None, err
    if "type" not in data:
        return None, "typeがない"
    return data, None


def validate_goal(data: dict):
    if data.get("type") != GOAL_TYPE:
        return False, f"typeが不正: {data.get('type')}"

    if not isinstance(data.get("goal"), str) or not data["goal"].strip():
        return False, "goalがない"

    objectives = data.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        return False, "objectivesがない（空のリスト不可）"
    if not all(isinstance(o, str) and o.strip() for o in objectives):
        return False, "objectivesの要素は文字列である必要がある"

    if "tags" in data and not isinstance(data["tags"], list):
        return False, "tagsはリストである必要がある"

    return True, None


def handle_goal(text) -> tuple[dict | None, str | None]:
    """goal出力を安全に処理する（str / Response どちらも可）"""
    data, err = parse_goal_output(text)
    if err:
        return None, f"parse error: {err}"

    ok, msg = validate_goal(data)
    if not ok:
        return None, f"validate error: {msg}"

    data["tags"] = _normalize_tags(data.get("tags", []))  # 索引用に正規化
    return data, None


# ======================================================================== #
# assistant（assistant.txt）: web_search / summary / assist
# ======================================================================== #
def parse_assistant_output(text) -> tuple[dict | None, str | None]:
    data, err = _extract_json(_to_text(text))
    if err:
        return None, err
    if "type" not in data:
        return None, "typeがない"
    return data, None


def validate_assistant(data: dict):
    t = data.get("type")

    if t not in ASSISTANT_TYPES:
        return False, f"typeが不正: {t}"

    if t == "web_search" and "query" not in data:
        return False, "queryがない"

    if t == "summary" and "conclusion" not in data:
        return False, "conclusionがない"

    if t == "assist" and "message" not in data:
        return False, "messageがない"

    return True, None


def handle_assistant(text) -> tuple[dict | None, str | None]:
    """assistant出力を安全に処理する（str / Response どちらも可）"""
    data, err = parse_assistant_output(text)
    if err:
        return None, f"parse error: {err}"

    ok, msg = validate_assistant(data)
    if not ok:
        return None, f"validate error: {msg}"

    return data, None


# ======================================================================== #
# task / action（system.txt）: command / file / code / assist
# ======================================================================== #
def parse_task_output(text) -> tuple[dict | None, str | None]:
    data, err = _extract_json(_to_text(text))
    if err:
        return None, err
    if "type" not in data:
        return None, "typeがない"
    if "reason" not in data:
        return None, "reasonがない"
    return data, None


def validate_task(data: dict):
    t = data.get("type")

    if t not in TASK_TYPES:
        return False, f"typeが不正: {t}"

    if t == "command":
        if "command" not in data:
            return False, "commandがない"
        if not str(data.get("command", "")).strip():
            return False, "commandが空"

    if t == "file":
        # action は任意（省略時は write）。path と content（read以外）が要件。
        if "path" not in data:
            return False, "pathがない"
        act = data.get("action", "write")
        if act in ("write", "append") and "content" not in data:
            return False, "contentがない"

    if t == "code":
        required = ["language", "code"]
        for r in required:
            if r not in data:
                return False, f"{r}がない"

    if t == "assist":
        if "message" not in data:
            return False, "messageがない"
        if not str(data.get("message", "")).strip():
            return False, "messageが空"

    if t == "web_search":
        if "query" not in data:
            return False, "queryがない"

    if t == "ssh_connect":
        for r in ("host", "user"):
            if r not in data:
                return False, f"{r}がない"

    if t == "tool":
        if "name" not in data:
            return False, "nameがない"

    return True, None


def handle_task(text) -> tuple[dict | None, str | None]:
    """system出力（action）を安全に処理する（str / Response どちらも可）"""
    data, err = parse_task_output(text)
    if err:
        return None, f"parse error: {err}"

    ok, msg = validate_task(data)
    if not ok:
        return None, f"validate error: {msg}"

    return data, None


if __name__ == "__main__":
    goal = '```json\n{"type":"goal","goal":"ログ解析レポート作成","objectives":["形式を調べる","解析する"],"tags":["Log","python","Log"]}\n```'
    print("goal OK :", handle_goal(goal))            # tagsが小文字化・重複除去される
    print("goal NG :", handle_goal('{"type":"goal","goal":"x"}'))  # objectives無し
    print("task OK :", handle_task('{"type":"command","command":"dir","reason":"確認"}'))
    print("asst OK :", handle_assistant('{"type":"summary","conclusion":"成功","points":["a"]}'))


# ====================================================================== #
# 契約ヒント: roleごとの「正しいJSONの形」を1行で説明（繰り返し是正の説明に使う）
# ====================================================================== #
_CONTRACT = {
    "goal": 'goal役: {"type":"goal","goal":"1~2行","objectives":["順番の目的",...],"tags":["英小文字の短い語"],"reason":"根拠"}',
    "plan": 'system役: typeは command/file/code/assist のどれか＋"reason"必須。'
            'command→"command" / file→"action","path","content" / code→"language","code" / assist→"message"',
    "system": 'system役: typeは command/file/code/assist＋"reason"必須。'
              'command→"command" / file→"action","path","content" / code→"language","code" / assist→"message"',
    "summary": 'assistant役: web_search→{"type":"web_search","query","reason"} / '
               'summary→{"type":"summary","conclusion","points":[...],"note"} / '
               'assist→{"type":"assist","message","reason"}',
    "judge": 'judge役: {"type":"judge","done":true/false,"reason":"判断の根拠"}',
    "assistant": 'assistant役: web_search→{"type":"web_search","query","reason"} / '
                 'summary→{"type":"summary","conclusion","points":[...],"note"} / '
                 'assist→{"type":"assist","message","reason"}',
}


_MODE_ALIASES = ("system_pentest", "system_recon")


def contract_hint(role: str) -> str:
    """その役割で許されるJSONの形（必須キー）を1行で返す。未知roleは空文字。"""
    if role in _MODE_ALIASES:    # セキュリティ/偵察モードも system と同じ契約
        return _CONTRACT.get("system", "") + ' web_searchも可(query必須)。'
    return _CONTRACT.get(role, "")


# ====================================================================== #
# judge（完了判定）: {"type":"judge","done":true/false,"reason":"..."}
# ====================================================================== #
def handle_judge(text):
    data, err = _extract_json(_to_text(text))
    if err:
        return None, f"parse error: {err}"
    if data.get("type") != "judge":
        return None, f"validate error: typeが不正: {data.get('type')}"
    if not isinstance(data.get("done"), bool):
        return None, "validate error: done(true/false)がない"
    return data, None


def handle_reflect(text):
    """自己評価: {"type":"reflect","score":int,"lessons":[...]}"""
    data, err = _extract_json(_to_text(text))
    if err:
        return None, f"parse error: {err}"
    if data.get("type") != "reflect":
        return None, f"validate error: typeが不正: {data.get('type')}"
    if not isinstance(data.get("lessons"), list):
        return None, "validate error: lessons(配列)がない"
    data["score"] = int(data.get("score", 0) or 0)
    return data, None


def handle_steps(text):
    """多段階プラン: {"type":"steps","steps":[...]}"""
    data, err = _extract_json(_to_text(text))
    if err:
        return None, f"parse error: {err}"
    if not isinstance(data.get("steps"), list) or not data["steps"]:
        return None, "validate error: steps(配列)がない"
    return data, None


def handle_critic(text):
    """行動レビュー: {"type":"critic","ok":bool,"advice":str}"""
    data, err = _extract_json(_to_text(text))
    if err:
        return None, f"parse error: {err}"
    if not isinstance(data.get("ok"), bool):
        return None, "validate error: ok(true/false)がない"
    return data, None


def handle_skill(text):
    """スキル蒸留: {"type":"skill","name","description","steps":[...],"tags":[...]}"""
    data, err = _extract_json(_to_text(text))
    if err:
        return None, f"parse error: {err}"
    if "steps" not in data or not isinstance(data["steps"], list):
        return None, "validate error: steps(配列)がない"
    return data, None
