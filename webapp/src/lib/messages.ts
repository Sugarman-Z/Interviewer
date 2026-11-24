/**
 * 跨层共享的用户可见文案单点。
 * 岗位空 400 这句同时存在于：服务端 gateway（Python，各持一份）、本文件
 *（webapp 内单点——wizardLogic 客户端校验与 Mock 路由 400 兜底同 import）。
 * 「webapp 内部同句」由构造保证；Python↔TS 跨语言无法共享，靠两侧测试
 * 硬拷贝字面量互钉（wizardLogic.test / test_web_contract 各自断言全句）——
 * 此前三处硬拷贝曾全程漂移过（逗号 vs 破折号）而双侧套件全绿，同句性质
 * 不能只靠约定。
 */
export const POSITION_REQUIRED_MSG = "岗位还没填——教练需要它来为你出题";
