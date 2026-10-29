import * as React from "react"
import { cn } from "@/lib/utils"

interface TooltipContextValue {
  open: boolean
  setOpen: (v: boolean) => void
}

const TooltipContext = React.createContext<TooltipContextValue>({
  open: false,
  setOpen: () => {},
})

/** Wrap multiple Tooltip instances — no-op passthrough. */
function TooltipProvider({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

function Tooltip({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false)
  return (
    <TooltipContext.Provider value={{ open, setOpen }}>
      <div className="relative inline-flex">{children}</div>
    </TooltipContext.Provider>
  )
}

function TooltipTrigger({
  children,
  asChild,
}: {
  children: React.ReactNode
  asChild?: boolean
}) {
  const { setOpen } = React.useContext(TooltipContext)
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<Record<string, unknown>>, {
      onMouseEnter: () => setOpen(true),
      onMouseLeave: () => setOpen(false),
      onFocus: () => setOpen(true),
      onBlur: () => setOpen(false),
    })
  }
  return (
    <div
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      className="inline-flex"
    >
      {children}
    </div>
  )
}

function TooltipContent({
  children,
  className,
  side = "top",
}: {
  children: React.ReactNode
  className?: string
  side?: "top" | "bottom" | "left" | "right"
}) {
  const { open } = React.useContext(TooltipContext)
  if (!open) return null

  const positionClass =
    side === "bottom"
      ? "top-full mt-2 left-1/2 -translate-x-1/2"
      : side === "left"
        ? "right-full mr-2 top-1/2 -translate-y-1/2"
        : side === "right"
          ? "left-full ml-2 top-1/2 -translate-y-1/2"
          : "bottom-full mb-2 left-1/2 -translate-x-1/2"

  return (
    <div
      className={cn(
        "absolute z-50 rounded-md bg-[#1e222d] border border-[#363a45] px-3 py-1.5 text-xs text-white shadow-lg whitespace-nowrap",
        positionClass,
        className
      )}
    >
      {children}
    </div>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
