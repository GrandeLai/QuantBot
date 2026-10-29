import { cn } from "@/lib/utils"

interface SliderProps {
  value: number[]
  onValueChange: (value: number[]) => void
  min?: number
  max?: number
  step?: number
  className?: string
  disabled?: boolean
}

function Slider({
  value,
  onValueChange,
  min = 0,
  max = 100,
  step = 1,
  className,
  disabled = false,
}: SliderProps) {
  const pct = ((value[0] - min) / (max - min)) * 100

  return (
    <div className={cn("relative flex w-full touch-none select-none items-center py-1", className)}>
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-[#2a2e39]">
        <div
          className="absolute h-full bg-[#2962ff] rounded-full"
          style={{ width: `${pct}%` }}
        />
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value[0]}
        disabled={disabled}
        onChange={(e) => onValueChange([Number(e.target.value)])}
        className="absolute inset-0 w-full cursor-pointer opacity-0"
      />
      {/* Thumb indicator */}
      <div
        className="absolute h-3.5 w-3.5 rounded-full border-2 border-[#2962ff] bg-[#1e222d] shadow transition-colors focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
        style={{ left: `calc(${pct}% - 7px)` }}
      />
    </div>
  )
}

export { Slider }
