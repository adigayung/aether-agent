import { ref } from "vue";

// Kode editor (Monaco) — dibuka dari File Explorer / panel CHANGES.
// `editorFile` = { path, name } dari Explorer (path relatif, bukan path baru).
// Tidak ada penulisan file dari browser: semua lewat API file backend existing.
// `key` per path (di template) memastikan instance Monaco tidak bocor antar file.
export function useCodeEditor({ setError }) {
  const editorOpen = ref(false);
  const editorFile = ref(null);

  function openFileInEditor(file) {
    const path = file && (file.path || file.file_path);
    if (!path) return;
    setError("");
    editorFile.value = { path, name: (file && file.name) || String(path).split("/").pop() };
    editorOpen.value = true;
  }

  function closeCodeEditor() {
    editorOpen.value = false;
    editorFile.value = null;
  }

  // Feedback error editor memakai mekanisme error banner AETHER yang sudah ada.
  function onEditorError(message) {
    setError(message || "Editor error.");
  }

  return { editorOpen, editorFile, openFileInEditor, closeCodeEditor, onEditorError };
}
