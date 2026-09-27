import type { Analysis, Animal } from './api';

/**
 * Animals in the frame on screen at `ms`. Mirrors engine.animals_at: the video shows
 * each frame until the next one starts, including through a camera dropout (a frozen
 * picture), so its boxes stay that long; after the last frame, only briefly.
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
  if (index < 0) return [];
  const tolerance = Math.max(100, 2000 / (analysis.fps || 30));
  if (index + 1 < timestamps.length || ms - timestamps[index] <= tolerance) return analysis.frames[index].animals;
  return [];
}

/**
 * Where the camera sent no frames and the picture froze. Mirrors tracking.PauseClock:
 * no frame for four frame times and at least 300 ms.
 */
export function dropouts(timestamps: number[], fps: number): { start: number; end: number }[] {
  const frameMs = 1000 / (fps > 0 ? fps : 15);
  const found = [];
  for (let i = 1; i < timestamps.length; i++) {
    if (timestamps[i] - timestamps[i - 1] > Math.max(4 * frameMs, 300)) found.push({ start: timestamps[i - 1], end: timestamps[i] });
  }
  return found;
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
