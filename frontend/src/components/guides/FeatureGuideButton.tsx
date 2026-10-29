/**
 * Floating trigger for feature guides.
 */
import { useState } from "react";
import { CircleHelp } from "lucide-react";
import { cn } from "@/lib/utils";
import { getFeatureGuide, type FeatureGuideKey } from "@/content/featureGuides";
import FeatureGuideModal from "./FeatureGuideModal";

interface FeatureGuideButtonProps {
  guideKey: FeatureGuideKey;
  className?: string;
}

export default function FeatureGuideButton({
  guideKey,
  className,
}: FeatureGuideButtonProps) {
  const guide = getFeatureGuide(guideKey);
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "guide-fab group absolute z-20 flex h-12 min-w-12 items-center gap-2 rounded-full border border-sky-400/20 bg-slate-950/85 px-3 text-sky-50 shadow-[0_18px_40px_rgba(8,15,33,0.42)] transition-all hover:-translate-y-0.5 hover:border-sky-300/45 hover:bg-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/60",
          "bottom-4 right-4",
          className,
        )}
        aria-label={`查看${guide.title}`}
        title={`查看${guide.title}`}
      >
        <span className="guide-fab-ring absolute inset-0 rounded-full" aria-hidden />
        <span className="relative flex h-7 w-7 items-center justify-center rounded-full bg-sky-400/16 text-sky-200 transition-colors group-hover:bg-sky-400/22">
          <CircleHelp className="h-4 w-4" />
        </span>
        <span className="relative text-xs font-semibold tracking-wide text-slate-100">指南</span>
      </button>

      <FeatureGuideModal guide={guide} open={open} onClose={() => setOpen(false)} />
    </>
  );
}
