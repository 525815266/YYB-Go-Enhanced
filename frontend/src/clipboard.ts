export async function copyText(value: string): Promise<void> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      /* use the selection fallback below */
    }
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.readOnly = true;
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.append(input);
  input.select();
  let copied = false;
  try { copied = document.execCommand("copy"); }
  finally { input.remove(); }
  if (!copied) throw new Error("浏览器未允许复制，请手动选择文本");
}
