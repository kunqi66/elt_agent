/// <reference types="vite/client" />

/** 前端可用的环境变量（.env.development / .env.production） */
interface ImportMetaEnv {
  /** 后端 API 前缀，例如 /api/v1 */
  readonly VITE_API_BASE_URL: string
  /**
   * dev mock 开关（字符串 'true' / 'false'）。
   * 生产构建里恒为 'false'——mock 数据若混进审计视图会污染证据口径。
   */
  readonly VITE_USE_MOCK: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
