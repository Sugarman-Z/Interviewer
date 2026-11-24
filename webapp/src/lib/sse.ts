/**
 * SSE 帧解析纯函数（wire 合同 = docs/contract/stream-events.sample.json，
 * 服务端帧 = event 行 + 单行 JSON data，payload 自带 event 键）。
 * 无状态设计：跨 chunk 断帧由 rest 回传解决——事件只在空行分隔符处完整，
 * 尾部无分隔符的内容原样作为 rest 留给下一片拼前缀，调用方不需携带任何
 * 解析器内部状态。
 */
import type { StreamEvent } from "./types";

export interface ParseResult {
  events: StreamEvent[];
  /** 尾部不完整帧（未见空行分隔符）——下一片到达后与之为前缀拼接 */
  rest: string;
}

export function parseSSE(input: string): ParseResult {
  // CRLF / 裸 CR 统一为 LF（SSE 规范三种行终止符等价）。归一化幂等：rest
  // 回传后与新片一起再归一化，语义不变（chunk 边界劈开 \r\n 时，前片尾
  // 的 \r 已成 LF，后片首的 \n 归一化后是空行，恰好补齐分隔符）
  const text = input.replace(/\r\n?/g, "\n");
  const sep = text.lastIndexOf("\n\n");
  if (sep === -1) {
    return { events: [], rest: text };
  }
  const events: StreamEvent[] = [];
  for (const frameText of text.slice(0, sep).split("\n\n")) {
    const parsed = parseFrame(frameText);
    if (parsed !== null) {
      events.push(parsed);
    }
  }
  return { events, rest: text.slice(sep + 2) };
}

/**
 * 单帧 → 事件。按 SSE 规范：`:` 开头的注释行丢弃；`data:` 多行以 \n 连接；
 * 无 data 字段的帧不派发；字段名后至多剥一个空格。event/id/retry 等字段
 * 解析但不参与派发——本题 wire 的 data 载荷自描述，事件名以 data 内的
 * event 键为准（event 行是规范的冗余表达）。
 * data 不是合法 JSON 的帧整帧丢弃（不抛）——合同外输入（代理注入等）的
 * 加固：坏帧若恰为终局，「resolve 未见终局」即上层对账信号；若为 delta，
 * 终局事件的整题权威替换本就覆盖。同批其余好帧照常派发（原样上抛会把
 * 同批已解析的好事件一起丢掉）。
 */
function parseFrame(frameText: string): StreamEvent | null {
  const dataLines: string[] = [];
  for (const line of frameText.split("\n")) {
    if (line === "" || line.startsWith(":")) {
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "data") {
      dataLines.push(value);
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    return JSON.parse(dataLines.join("\n")) as StreamEvent;
  } catch {
    return null;
  }
}
