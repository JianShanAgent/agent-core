import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** 拼 class：后面的同类样式盖住前面的。 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** 按路径取嵌套对象里的值。 */
export function getAt(node: unknown, path: readonly string[]): unknown {
  let current = node;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

/** 按路径改嵌套对象里的值，返回新对象（原对象不动）。`value` 为 undefined 表示删掉这一项。 */
export function setAt<T>(node: T, path: readonly string[], value: unknown): T {
  if (path.length === 0) return value as T;
  const [head, ...rest] = path;
  const base: Record<string, unknown> =
    node !== null && typeof node === "object" && !Array.isArray(node)
      ? { ...(node as Record<string, unknown>) }
      : {};
  if (rest.length === 0) {
    if (value === undefined) delete base[head];
    else base[head] = value;
  } else {
    base[head] = setAt(base[head], rest, value);
  }
  return base as T;
}

/** 两个 JSON 值是不是一样（配置的默认值和现在的值比较用）。8 和 8.0 算一样。 */
export function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== typeof b) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((item, index) => sameValue(item, b[index]));
  }
  if (typeof a === "object") {
    const left = a as Record<string, unknown>;
    const right = b as Record<string, unknown>;
    const keys = Object.keys(left);
    if (keys.length !== Object.keys(right).length) return false;
    return keys.every((key) => key in right && sameValue(left[key], right[key]));
  }
  return false;
}

export function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  // 局域网里用 http 打开时没有 clipboard 接口：退回老办法
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
  return Promise.resolve();
}
