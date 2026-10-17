/**
 * Element Plus 命令式反馈 API 的统一出口。
 *
 * 为什么不让 auto-import 接管 ElMessage：
 * 它们是**在 script 里当作值调用**的，不像模板里的 `<el-button>` 那样宽松。
 * auto-import 生成的 `auto-imports.d.ts` 要等构建过一次才存在，
 * 在那之前 `vue-tsc --noEmit` 会直接报「找不到名称 ElMessage」。
 * 显式导入 + 这里集中引一次样式，既让类型检查不依赖构建产物，
 * 也避免每个文件各写一遍组件的 style/css 子路径。
 *
 * 模板里的组件（el-button / el-table…）仍走 vite.config.ts 的按需解析，不在这里管。
 */

import { ElMessage, ElMessageBox, ElLoading } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'

export { ElMessage, ElMessageBox, ElLoading }
