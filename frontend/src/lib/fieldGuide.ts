// Field-guide photos for a species from English Wikipedia, loaded by the browser.
// Deliberately not from the gallery: its photos carry the AI's labels, which are
// what the answer key measures.

export interface GuidePhoto {
  src: string;
  /** Female, Young or Male when the photo's name or caption says so. */
  label: string;
}

export interface GuideEntry {
  species: string;
  /** Wikipedia's short description, e.g. "Species of bird in North America". */
  description: string;
  page: string;
  photos: GuidePhoto[];
}

interface MediaItem {
  title: string;
  type: string;
  showInGallery?: boolean;
  caption?: { text?: string };
  srcset?: { src: string; scale: string }[];
}

const API = 'https://en.wikipedia.org/api/rest_v1/page';
const RASTER = /\.(jpe?g|png|webp)$/i;
// Article images that won't help at a feeder: maps, eggs, nests, newborns, artwork.
const NOT_THE_ANIMAL = /\b(egg|eggs|nest|nests|nestlings?|hatchlings?|newborn|hairless|map|range|skeleton|skull|specimen|stamp|illustration|drawing|plate|audubon|painting|coin)\b/i;
const MAX_PHOTOS = 3;
const cache = new Map<string, Promise<GuideEntry | null>>();

function label(text: string): string {
  if (/female/i.test(text)) return 'Female';
  if (/juvenile|immature|young|fledgling|first[\s_-]?winter/i.test(text)) return 'Young';
  if (/\bmale\b|_male[_.]/i.test(text)) return 'Male';
  return '';
}

function fileName(url: string): string {
  const path = new URL(url, location.href).pathname.split('/');
  // Thumbnails end in /<width>px-<name>; originals in /<name>.
  const last = decodeURIComponent(path[path.length - 1]).replace(/^\d+px-/, '');
  return last.replace(/ /g, '_');
}

async function load(species: string): Promise<GuideEntry | null> {
  const title = encodeURIComponent(species.replace(/ /g, '_'));
  const [summary, media] = await Promise.all([
    fetch(`${API}/summary/${title}`).then((r) => (r.ok ? r.json() : null)),
    fetch(`${API}/media-list/${title}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  if (!summary?.thumbnail?.source) return null;
  const lead = fileName(summary.thumbnail.source);
  const items: MediaItem[] = (media?.items ?? []).filter((item: MediaItem) =>
    item.type === 'image' && item.showInGallery !== false && RASTER.test(item.title)
    && !NOT_THE_ANIMAL.test(`${item.title} ${item.caption?.text ?? ''}`.replace(/_/g, ' ')) && item.srcset?.length);
  const describe = (item: MediaItem) => label(`${item.title} ${item.caption?.text ?? ''}`);
  const leadItem = items.find((item) => item.title.replace(/^File:/, '').replace(/ /g, '_') === lead);
  const photos: GuidePhoto[] = [{ src: summary.thumbnail.source, label: leadItem ? describe(leadItem) : '' }];
  const others = items.filter((item) => item !== leadItem);
  // Labelled photos first (females and young look most different), then one more view.
  const labelled = others.filter((item) => describe(item) && describe(item) !== photos[0].label);
  const extra = [...labelled, ...others.filter((item) => !describe(item)).slice(0, 1)];
  for (const item of extra) {
    if (photos.length >= MAX_PHOTOS) break;
    const text = describe(item);
    if (text && photos.some((photo) => photo.label === text)) continue;
    const source = item.srcset![0].src;
    photos.push({ src: source.startsWith('//') ? `https:${source}` : source, label: text });
  }
  return { species, description: summary.description ?? '', page: summary.content_urls?.desktop?.page ?? '', photos };
}

/** The species' field-guide entry, or null when Wikipedia has no photo for that name. */
export function fieldGuide(species: string): Promise<GuideEntry | null> {
  let entry = cache.get(species);
  if (!entry) {
    entry = load(species).catch(() => {
      cache.delete(species); // offline or rate-limited: try again next time
      return null;
    });
    cache.set(species, entry);
  }
  return entry;
}
