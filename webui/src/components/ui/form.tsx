import { Label as LabelPrimitive, Switch as SwitchPrimitive } from "radix-ui";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const field =
  "border-input bg-card placeholder:text-muted-foreground/70 w-full min-w-0 rounded-md border text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive";

function Input({ className, type, ...props }: ComponentProps<"input">) {
  return <input type={type} className={cn(field, "h-9 px-3 py-1", className)} {...props} />;
}

function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return (
    <textarea className={cn(field, "min-h-20 px-3 py-2 leading-relaxed", className)} {...props} />
  );
}

function Label({ className, ...props }: ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      className={cn("text-sm leading-none font-medium select-none", className)}
      {...props}
    />
  );
}

function Switch({ className, ...props }: ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "peer data-[state=checked]:bg-brand data-[state=unchecked]:bg-input inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent shadow-xs transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="pointer-events-none block size-4 rounded-full bg-white shadow-sm ring-0 transition-transform data-[state=checked]:translate-x-[18px] data-[state=unchecked]:translate-x-0.5" />
    </SwitchPrimitive.Root>
  );
}

export { Input, Label, Switch, Textarea };
