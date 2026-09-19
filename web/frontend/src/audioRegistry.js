// Audio Registry — centralized sound configuration for AETHER.
//
// Maps terminal task statuses to audio asset paths.
// Asset paths are relative to the project root; Vite serves
// files under `assets/` at `/assets/` in dev and build.
//
// Adding a new sound: add an entry here and wire it in
// App.vue handleEvent(). No other file needs to change.

const AUDIO_MAP = {
  completed: "/assets/audio/succeed.wav",
  failed: "/assets/audio/failed.wav",
  cancelled: "/assets/audio/stop.wav",
};

let audioCtx = null;
let lastPlayedStatus = null;

function getAudioContext() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return audioCtx;
}

/**
 * Play the sound associated with a terminal task status.
 * Deduplicates: same status in a row is ignored.
 * Returns silently if the status has no registered sound.
 */
export function playStatusSound(status) {
  const s = (status || "").toLowerCase();
  if (s === lastPlayedStatus) return;
  const src = AUDIO_MAP[s];
  if (!src) return;

  lastPlayedStatus = s;
  try {
    const ctx = getAudioContext();
    const audio = new Audio(src);
    audio.addEventListener("ended", () => {
      // Allow the same status to play again after the sound finishes
      // (e.g. user starts a new task that also completes).
      lastPlayedStatus = null;
    });
    audio.play().catch(() => {
      // Autoplay may be blocked by browser; silently ignore.
      lastPlayedStatus = null;
    });
  } catch {
    lastPlayedStatus = null;
  }
}

/** Reset the deduplication tracker (useful when a new task starts). */
export function resetAudioTracker() {
  lastPlayedStatus = null;
}

/** Expose the registry for inspection (no mutation). */
export const audioRegistry = Object.freeze({ ...AUDIO_MAP });
