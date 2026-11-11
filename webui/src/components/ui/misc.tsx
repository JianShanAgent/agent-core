import { LoaderCircle } from "lucide-react";
import { Tooltip as TooltipPrimitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("bg-muted animate-pulse rounded-md", className)} {...props} />;
}

function Separator({ className, vertical = false }: { className?: string; vertical?: boolean }) {
  return (
    <div
      role="separator"
      className={cn("bg-border shrink-0", vertical ? "h-full w-px" : "h-px w-full", className)}
    />
  );
}

function Spinner({ className }: { className?: string }) {
  return <LoaderCircle className={cn("text-muted-foreground size-4 animate-spin", className)} />;
}

const TooltipProvider = TooltipPrimitive.Provider;

/** 鼠标停上去显示一句说明。 */
function Hint({
  text,
  children,
  side = "top",
}: {
  text: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
}) {
  if (!text) return <>{children}</>;
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={5}
          className="bg-foreground text-background animate-in z-50 max-w-xs rounded-md px-2.5 py-1.5 text-xs leading-relaxed shadow-md"
        >
          {text}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

function Table({ className, ...props }: ComponentProps<"table">) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full caption-bottom text-sm", className)} {...props} />
    </div>
  );
}

function Th({ className, ...props }: ComponentProps<"th">) {
  return (
    <th
      className={cn(
        "text-muted-foreground h-9 border-b px-3 text-left text-xs font-medium whitespace-nowrap",
        className,
      )}
      {...props}
    />
  );
}

function Td({ className, ...props }: ComponentProps<"td">) {
  return <td className={cn("border-b px-3 py-2 align-middle", className)} {...props} />;
}

function Tr({ className, ...props }: ComponentProps<"tr">) {
  return (
    <tr
      className={cn("hover:bg-muted/50 transition-colors [&:last-child>td]:border-b-0", className)}
      {...props}
    />
  );
}

/** 一段等宽的原文（提示词、YAML、JSON）。 */
function Pre({ className, ...props }: ComponentProps<"pre">) {
  return (
    <pre
      className={cn(
        "bg-muted/60 overflow-x-auto rounded-md border p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap",
        className,
      )}
      {...props}
    />
  );
}

export { Hint, Pre, Separator, Skeleton, Spinner, Table, Td, Th, TooltipProvider, Tr };
