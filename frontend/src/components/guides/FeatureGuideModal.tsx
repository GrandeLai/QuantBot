/**
 * Shared animated modal for feature guides.
 */
import { useEffect } from "react";
import { AnimatePresence, motion } from "motion/react";
import { BookOpenText, CircleHelp, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { FeatureGuide } from "./guideTypes";

interface FeatureGuideModalProps {
  guide: FeatureGuide;
  open: boolean;
  onClose: () => void;
}

export default function FeatureGuideModal({
  guide,
  open,
  onClose,
}: FeatureGuideModalProps) {
  useEffect(() => {
    if (!open) return undefined;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/72 px-4 py-6 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="max-h-[90vh] w-full max-w-3xl overflow-hidden rounded-[28px] border border-white/10 bg-[#0f1624] shadow-[0_32px_120px_rgba(2,6,23,0.7)]"
            initial={{ opacity: 0, y: 28, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.97 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="relative overflow-hidden border-b border-white/8 bg-[radial-gradient(circle_at_top_right,_rgba(56,189,248,0.28),_transparent_36%),linear-gradient(135deg,#162033_0%,#0f1624_50%,#0c1220_100%)] px-6 py-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <div className="inline-flex items-center gap-2 rounded-full border border-sky-400/25 bg-sky-400/10 px-3 py-1 text-[11px] font-semibold tracking-[0.18em] text-sky-100 uppercase">
                  <BookOpenText className="h-3.5 w-3.5" />
                  操作指南
                </div>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={onClose}
                  className="rounded-full border border-white/10 bg-white/5 text-slate-300 hover:bg-white/10 hover:text-white"
                  aria-label="关闭操作指南"
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>

              <div className="max-w-2xl">
                <h2 className="text-2xl font-semibold tracking-tight text-white">{guide.title}</h2>
                <p className="mt-3 text-sm leading-6 text-slate-300">{guide.summary}</p>
                <div className="mt-4 inline-flex items-start gap-2 rounded-2xl border border-emerald-400/20 bg-emerald-400/10 px-4 py-3 text-sm text-emerald-100">
                  <CircleHelp className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{guide.goal}</span>
                </div>
              </div>
            </div>

            <div className="custom-scrollbar max-h-[calc(90vh-200px)] overflow-y-auto px-6 py-6">
              <div className="space-y-4">
                {guide.steps.map((step, index) => (
                  <motion.section
                    key={`${guide.title}-${step.title}`}
                    className="rounded-[24px] border border-white/8 bg-white/[0.03] p-5"
                    initial={{ opacity: 0, y: 16 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.04 * index, duration: 0.18 }}
                  >
                    <div className="flex gap-4">
                      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-sky-400/14 text-sm font-bold text-sky-100">
                        {index + 1}
                      </div>
                      <div className="min-w-0 flex-1">
                        <h3 className="text-base font-semibold text-white">{step.title}</h3>
                        <p className="mt-2 text-sm leading-6 text-slate-300">{step.description}</p>
                        {step.tips?.length ? (
                          <div className="mt-3 flex flex-wrap gap-2">
                            {step.tips.map((tip) => (
                              <span
                                key={tip}
                                className="rounded-full border border-white/8 bg-white/[0.04] px-3 py-1 text-xs text-slate-300"
                              >
                                {tip}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </motion.section>
                ))}
              </div>

              {guide.quickTips?.length ? (
                <section className="mt-6 rounded-[24px] border border-amber-400/16 bg-amber-400/10 p-5">
                  <div className="text-xs font-semibold uppercase tracking-[0.18em] text-amber-200">
                    小贴士
                  </div>
                  <div className="mt-3 space-y-2 text-sm leading-6 text-amber-50">
                    {guide.quickTips.map((tip) => (
                      <p key={tip}>{tip}</p>
                    ))}
                  </div>
                </section>
              ) : null}
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
