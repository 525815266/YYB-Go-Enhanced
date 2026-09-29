import { describe, expect, it } from "vitest";
import { parseResponse } from "./api";
describe("API response parsing", () => {
  it("unwraps envelopes and preserves raw protocol payloads", () => {
    expect(parseResponse('{"code":0,"msg":"success","data":[1]}', 200)).toEqual(
      [1],
    );
    expect(parseResponse('{"code":"wechat-code"}', 200)).toEqual({
      code: "wechat-code",
    });
  });
  it("reports empty/truncated responses and business failures", () => {
    expect(() => parseResponse("", 200)).toThrow("空响应");
    expect(() => parseResponse("{", 502)).toThrow("非 JSON");
    expect(() =>
      parseResponse('{"code":403,"msg":"需要管理员权限","data":null}', 403),
    ).toThrow("需要管理员权限");
    expect(() =>
      parseResponse('{"code":1,"msg":"操作失败","data":null}', 200),
    ).toThrow("操作失败");
  });
});
