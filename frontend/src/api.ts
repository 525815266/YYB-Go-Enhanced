export class APIError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export function parseResponse(text: string, status: number): unknown {
  if (!text.trim())
    throw new APIError(`服务返回空响应（HTTP ${status}）`, status);
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new APIError(`服务返回了非 JSON 内容（HTTP ${status}）`, status);
  }
  const envelope =
    payload &&
    typeof payload === "object" &&
    "code" in payload &&
    "msg" in payload &&
    "data" in payload;
  if (status < 200 || status >= 300 || (envelope && payload.code !== 0)) {
    throw new APIError(payload?.msg || `请求失败（HTTP ${status}）`, status);
  }
  return envelope ? payload.data : payload;
}

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    if (response.status === 401 && path !== "/login")
      window.dispatchEvent(new Event("yyb-session-expired"));
    return parseResponse(text, response.status) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw new Error("请求超时，请重试");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export interface Account {
  id: number;
  openid: string;
  remark?: string;
  nickname?: string;
  alias?: string;
  status: string;
  last_checked_at?: number;
  credential_expires_in?: number;
}
export interface User {
  id: number;
  username: string;
  display_name: string;
  role: string;
  enabled: boolean;
  account_count?: number;
  login_count?: number;
  last_login_at?: string;
}
export const accountName = (account: Account) =>
  account.remark || account.nickname || account.alias || `账号 ${account.id}`;
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败";
