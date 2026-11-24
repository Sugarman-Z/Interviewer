"""结构化输出 + 弱模型降级。method 必须显式透传：智谱端点 json_schema 全炸
（冒烟证据 scripts/data/structured_smoke_results.json），不给 langchain 默认值机会。
"""
import json
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


def invoke_structured_or_freetext(
    llm, schema, prompt: str, method: str | None = None, max_retries: int = 1
):
    """两层降级：先 with_structured_output（method 显式透传，None=langchain 默认
    不推荐）；失败/None → 同一 prompt 免费文本重试 + 手工解析 JSON。
    仍失败返回 None，调用方走确定性保守出口（绝不编造默认值）。
    """
    try:
        kwargs = {"method": method} if method else {}
        structured = llm.with_structured_output(schema, **kwargs)
        result = structured.invoke(prompt)
        if result is not None:
            return result
    except Exception as exc:  # noqa: BLE001 —— 降级路径，失败可见即可
        logger.warning("结构化输出失败，降级为自由文本：%s", exc)

    # 降级 prompt 必须自包含：schema 是 langchain 在第一条路径里注入的，
    # freetext 这条新调用里模型根本看不到 schema，不显式带上就无从遵约。
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    fallback_tail = (
        "\n\n严格按以下 JSON Schema 输出一个 JSON 对象，不要输出任何其他内容：\n"
        + schema_json
    )
    for _ in range(max_retries + 1):
        try:
            resp = llm.invoke(prompt + fallback_tail)
            data = json.loads(_extract_json(getattr(resp, "content", "")))
            return schema(**data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自由文本降级解析失败：%s", exc)
    return None


def _extract_json(text: str) -> str:
    """从可能带 ```json 围栏的文本里抠出 JSON 主体。"""
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip().removeprefix("json").strip()
            if candidate.startswith("{"):
                return candidate
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else text


class LoopTool(Protocol):
    """工具循环的 seam：as_tool() 出模型面（name = 分派键），execute(args) 出
    循环面，返回 (content, record)——content 回喂模型（空命中回哨兵串不回
    空串），record 留痕（None=不留），循环只补 "tool" 键。

    不变量：execute 异常原样上抛（降级归实现内部）；模型可见参数只由
    as_tool() 签名声明，硬约束走构造闭包；累积账本放实现属性上（适配器
    禁跨调用复用，旧命中混入对账会放行幻觉编号）；并行 tool_calls 按到达
    顺序逐个执行。
    """

    def as_tool(self): ...
    def execute(self, args: dict) -> tuple[str, dict | None]: ...


def invoke_structured_with_tools(
    llm,
    schema,
    prompt: str,
    tools: list,
    *,
    max_rounds: int = 3,
    max_calls: int | None = None,
):
    """工具 + 输出 schema 同绑一次调用的循环。bind_tools 直接绑 pydantic 类；
    不能改用 with_structured_output（与工具互占 function calling 通道）。

    返回 (result | None, trace)：本函数不重试不 raise，降级梯归调用方；
    trace 只给调用方落盘，绝不进 messages。失败分层：配置错误（空/缺面孔/
    重名/撞 schema 类名）当场炸；模型走神（坏 args/未知工具/只逛不交卷）
    回喂不炸；传输层失败（429/超时重试耗尽）→ (None, trace) 不炸；工具
    内部炸原样上抛。max_calls 是 execute 执行次数硬顶（GLM 单轮并行发多个
    tool_calls，轮数管不住执行数；触顶回话不执行），None/≤0 = 不限。

    军规：每个 tool_call 必回 ToolMessage（悬空调用端点直接报错）；工具消息
    只在局部 messages 流转，绝不写图 state；硬约束在实现闭包里。
    """
    from langchain_core.messages import HumanMessage, ToolMessage  # 惰性导入（第 6 式）

    tools = list(tools)
    if not tools:
        raise ValueError("tools 为空——无工具路径请走 invoke_structured_or_freetext")
    for t in tools:
        if not hasattr(t, "as_tool") or not hasattr(t, "execute"):
            raise TypeError(f"循环工具缺 as_tool/execute 两副面孔：{type(t).__name__}")

    faces = [t.as_tool() for t in tools]
    names = [getattr(f, "name", "") for f in faces]
    schema_name = schema.__name__
    if dup := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"工具重名，模型无法区分菜单项：{dup}")
    if clash := sorted(set(names) & {schema_name}):
        raise ValueError(f"工具名与输出 schema 类名相撞：{clash}（schema 名是交卷腿的保留名）")
    dispatch = dict(zip(names, tools))

    bound = llm.bind_tools([*faces, schema])
    # 首条必须 user 角色：智谱端点拒收 system-only 消息列表（400/1214，
    # scripts/probe_tool_messages.py 三形状探测定案）。老路径 invoke(str) 也是
    # 转 user——两条通道报文格式对齐，端点行为一致可预测
    messages = [HumanMessage(prompt)]
    trace: list = []
    last_content = ""
    executed = 0  # 调用数硬顶的计数器：只数实际执行（max_calls，

    for _ in range(max_rounds):
        try:
            resp = bound.invoke(messages)
        except Exception as exc:  # noqa: BLE001 —— 传输层失败族（429/超时，client 退避耗尽后）
            # 与 invoke_structured_or_freetext 的降级风格对齐：client 层的
            # 退避重试已经试过了，这里再试只是烧轮数——(None, trace) 交回
            # 调用方的降级梯（兜底池/熔断），本函数依旧不重试不 raise
            logger.warning("工具循环 LLM 调用失败（传输层），降级交回退梯：%s", exc)
            return None, trace
        last_content = getattr(resp, "content", "") or ""
        tool_calls = getattr(resp, "tool_calls", None) or []
        if not tool_calls:
            break  # 模型直接作答：出环走 content 解析腿

        messages.append(resp)
        schema_result = None
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args") or {}
            call_id = tc.get("id", "")
            handler = dispatch.get(name)
            if handler is not None:
            # 唯一执行口：max_calls 硬顶在这里计数。
                # 触顶腿回话不执行——文案去业务化（"检索"是 bank 的词，
                # C6 第二工具入环时不该读到别家的措辞）
                if max_calls is not None and max_calls > 0 and executed >= max_calls:
                    messages.append(ToolMessage(
                        content=f"工具调用次数已达上限（{max_calls} 次），请直接提交最终结果",
                        tool_call_id=call_id,
                    ))
                    continue
                executed += 1
                content, record = handler.execute(args)
                if record is not None:
                    record.setdefault("tool", name)  # C6 双工具时留痕天然可区分
                    trace.append(record)
                messages.append(ToolMessage(content=content, tool_call_id=call_id))
            elif name == schema_name:
                # 模型"填表交卷"：args 即表单内容，model_validate 即验收——
                # 校验失败记 None 继续：模型还在线上，这轮走神不代表下轮
                # 也走神（走神是正常路况，不是事故，绝不 raise）
                try:
                    schema_result = schema.model_validate(args)
                except Exception as exc:  # noqa: BLE001 —— 与本文件降级风格一致
                    schema_result = None
                    # 军规①对本腿同样成立：交了卷（哪怕不合格）也必须回票——
                    # 悬空票号会让下一轮请求直接协议错。回喂校验错误本身，
                    # 模型还能据以修正重交（同"未知工具"腿的纠错回喂）
                    messages.append(ToolMessage(
                        content=f"交卷校验失败：{exc}。请按 schema 修正后重新交卷",
                        tool_call_id=call_id,
                    ))
            else:
                # 军规①：幻觉菜也要回话（悬空票号=下次请求协议错），模型可继续
                messages.append(ToolMessage(content=f"未知工具：{name}", tool_call_id=call_id))

        if schema_result is not None:
            return schema_result, trace  # 腿①出环；累积型账本在各工具里，调用方自取

    # 腿②：content 解析（轮数耗尽 或 模型直接作答）。失败 → (None, trace)：
    # 不 raise 不重试——降级梯（兜底池/熔断）归调用方，一个函数只拥有一层失败语义
    try:
        return schema.model_validate(json.loads(_extract_json(last_content))), trace
    except Exception:  # noqa: BLE001
        return None, trace
