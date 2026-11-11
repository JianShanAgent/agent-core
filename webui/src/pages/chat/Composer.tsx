// 网页对话的输入区：多行输入框、加图片（选文件、直接粘贴，或者拖到页面上）、发送。

import { ImagePlus, Send, X } from "lucide-react";
import {
  type ChangeEvent,
  type ClipboardEvent,
  type KeyboardEvent,
  type Ref,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form";
import { Hint } from "@/components/ui/misc";

// 这几个上限和后端一致（admin/routes_chat.py）
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_TEXT_CHARS = 4000;
// 整个请求不能超过 24 MB（runtime.py 的 _MAX_REQUEST_BYTES），图片转成文字后会大三分之一。
// 留 1 MB 给消息本身，图片转码后加起来不能超过这么多字符
const MAX_IMAGE_CHARS = 23 * 1024 * 1024;
/** 输入框最高长到多少像素，再多就在框里滚 */
const MAX_HEIGHT = 168;

interface Picture {
  id: number;
  name: string;
  /** data URI，发给机器人的就是它 */
  source: string;
}

let serial = 0;

function readAsDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("读不出来"));
    reader.readAsDataURL(file);
  });
}

/** 页面能让输入区做的事：把拖到页面上的文件交给它。 */
export interface ComposerHandle {
  addFiles: (files: File[]) => void;
}

export function Composer({
  busy,
  onSend,
  ref,
}: {
  /** 上一条还没回完：可以接着打字，但不能发 */
  busy: boolean;
  onSend: (text: string, images: string[]) => void;
  ref?: Ref<ComposerHandle>;
}) {
  const [text, setText] = useState("");
  const [pictures, setPictures] = useState<Picture[]>([]);
  // 和 pictures 是同一份。读图片要等一会儿，读完得拿「现在」的那份来接着加，所以另外记一份随时能读的
  const held = useRef<Picture[]>([]);
  const area = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  const keep = (next: Picture[]) => {
    held.current = next;
    setPictures(next);
  };

  // 读图片要等一会儿：读完时如果已经离开这一页，就不往下做了
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // 输入框跟着内容长高
  useLayoutEffect(() => {
    const element = area.current;
    if (!element) return;
    element.style.height = "auto";
    // scrollHeight 不含上下边框
    element.style.height = `${Math.min(element.scrollHeight + 2, MAX_HEIGHT)}px`;
  }, [text]);

  const addFiles = async (files: File[]) => {
    const problems: string[] = [];
    const tooMany = (name: string) => `一条消息最多带 ${MAX_IMAGES} 张图，「${name}」没加上`;
    const loaded: Picture[] = [];
    let room = MAX_IMAGES - held.current.length;
    for (const file of files) {
      const name = file.name || "这张图";
      if (!file.type.startsWith("image/")) problems.push(`「${name}」不是图片`);
      else if (file.size > MAX_IMAGE_BYTES) problems.push(`「${name}」超过 10 MB`);
      else if (room <= 0) problems.push(tooMany(name));
      else {
        room -= 1;
        try {
          loaded.push({ id: ++serial, name, source: await readAsDataUri(file) });
        } catch {
          problems.push(`「${name}」读不出来`);
        }
      }
    }
    if (!alive.current) return;
    const next = [...held.current];
    let chars = next.reduce((sum, picture) => sum + picture.source.length, 0);
    for (const picture of loaded) {
      if (next.length >= MAX_IMAGES) {
        problems.push(tooMany(picture.name));
      } else if (chars + picture.source.length > MAX_IMAGE_CHARS) {
        problems.push(
          `加上「${picture.name}」图片就太大了（一条消息的图片加起来不能超过 17 MB 左右）`,
        );
      } else {
        chars += picture.source.length;
        next.push(picture);
      }
    }
    if (next.length !== held.current.length) keep(next);
    if (problems.length === 1) toast.error(problems[0]);
    else if (problems.length) toast.error("有几张图没加上", { description: problems.join("；") });
  };
  useImperativeHandle(ref, () => ({ addFiles: (files) => void addFiles(files) }));

  const submit = () => {
    if (busy) return;
    const body = text.trim();
    if (!body && pictures.length === 0) return;
    if (body.length > MAX_TEXT_CHARS) {
      toast.error(`消息太长了（最多 ${MAX_TEXT_CHARS} 个字，现在 ${body.length} 个）`);
      return;
    }
    onSend(
      body,
      pictures.map((picture) => picture.source),
    );
    setText("");
    keep([]);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    // 输入法组字时的回车是选字，不是发送。Safari 在组字结束后才发这个回车，
    // 那时 isComposing 已经是 false，要靠 keyCode 229 认出来
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit();
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
    if (files.length === 0) return;
    event.preventDefault();
    void addFiles(files);
  };

  const onPick = (event: ChangeEvent<HTMLInputElement>) => {
    const files = [...(event.target.files ?? [])];
    // 清掉选择：同一张图去掉后还能再选
    event.target.value = "";
    if (files.length) void addFiles(files);
  };

  const empty = !text.trim() && pictures.length === 0;

  return (
    <div className="flex flex-col gap-2">
      {pictures.length ? (
        <div className="flex flex-wrap gap-2">
          {pictures.map((picture) => (
            <div key={picture.id} className="relative">
              <img
                src={picture.source}
                alt={picture.name}
                title={picture.name}
                className="bg-muted size-16 rounded-md border object-cover"
              />
              <button
                type="button"
                onClick={() => keep(held.current.filter((item) => item.id !== picture.id))}
                className="bg-foreground text-background absolute -top-1.5 -right-1.5 flex size-5 cursor-pointer items-center justify-center rounded-full shadow-sm"
              >
                <X className="size-3" />
                <span className="sr-only">去掉这张图</span>
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="flex items-end gap-2">
        <input
          ref={picker}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={onPick}
        />
        <Hint
          text={`加图片：最多 ${MAX_IMAGES} 张，单张不超过 10 MB。也可以直接粘贴，或者把图片拖到页面上。`}
        >
          <Button
            variant="outline"
            size="icon"
            disabled={pictures.length >= MAX_IMAGES}
            onClick={() => picker.current?.click()}
          >
            <ImagePlus />
            <span className="sr-only">加图片</span>
          </Button>
        </Hint>
        <Textarea
          ref={area}
          rows={1}
          autoFocus
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={busy ? "等机器人回完这一条…" : "说点什么。回车发送，Shift + 回车换行"}
          className="min-h-9 resize-none py-1.5 leading-[22px]"
          style={{ maxHeight: MAX_HEIGHT }}
        />
        <Button variant="brand" loading={busy} disabled={empty} onClick={submit}>
          {busy ? (
            "处理中"
          ) : (
            <>
              <Send /> 发送
            </>
          )}
        </Button>
      </div>
    </div>
  );
}
