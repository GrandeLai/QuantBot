/**
 * Feature guide content model.
 */
export interface FeatureGuideStep {
  title: string;
  description: string;
  tips?: string[];
}

export interface FeatureGuide {
  title: string;
  summary: string;
  goal: string;
  steps: FeatureGuideStep[];
  quickTips?: string[];
}
