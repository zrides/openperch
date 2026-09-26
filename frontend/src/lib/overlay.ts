import type { Analysis, Animal } from './api';

/**
 * Animals detected in the decoded frame at `ms`. Mirrors engine.animals_at:
 * use the frame's own timestamp and never stretch boxes across a gap.
 */
export function animalsAt(analysis: Analysis, timestamps: number[], ms: number): Animal[] {
  // Last frame whose timestamp is <= ms + 1 (binary search).
  let low = 0;
  let high = timestamps.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (timestamps[mid] <= ms + 1) low = mid + 1;
    else high = mid;
  }
  const index = low - 1;
  const tolerance = Math.max(100, 2000 / (analysis.fps || 30));
  if (index >= 0 && Math.abs(ms - timestamps[index]) <= tolerance) return analysis.frames[index].animals;
  return [];
}

/** The rectangle a contained (letterboxed) video actually occupies inside its element. */
export function contentRect(element: { clientWidth: number; clientHeight: number }, videoWidth: number, videoHeight: number) {
  const width = element.clientWidth;
  const height = element.clientHeight;
  if (!videoWidth || !videoHeight || !width || !height) return { left: 0, top: 0, width, height };
  const scale = Math.min(width / videoWidth, height / videoHeight);
  const shownWidth = videoWidth * scale;
  const shownHeight = videoHeight * scale;
  return { left: (width - shownWidth) / 2, top: (height - shownHeight) / 2, width: shownWidth, height: shownHeight };
}

export function animalLabel(animal: Animal): string {
  return animal.display_label ?? animal.ranked[0]?.label ?? animal.category;
}
