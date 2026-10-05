import { computed, ref } from "vue";
// Versi AETHER dibaca dari SINGLE SOURCE OF TRUTH `data/version.json` (Vite
// meng-inline JSON saat build). TIDAK ada file versi kedua.
import versionInfo from "../../../../data/version.json";

// Navigasi berorientasi user (bukan subsystem internal AETHER).
// `icon` = path SVG (stroke) inline — tanpa dependency icon baru.
const navItems = [
  {
    id: "agent",
    label: "Workbench",
    icon: "M3 4h18v12H3zM8 20h8M12 16v4",
  },
  {
    id: "tasks",
    label: "Tasks",
    icon: "M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2",
  },
  {
    id: "projects",
    label: "Projects",
    icon: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  },
  {
    id: "backup",
    label: "Backup",
    icon: "M12 3v10M8 9l4 4 4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2",
  },
  {
    id: "extension",
    label: "Extension",
    icon: "M12 2l7 4v8l-7 4-7-4V6zM12 12v8M5 6l7 6 7-6",
  },
  {
    id: "settings",
    label: "Settings",
    icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.6 7a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H7a1.7 1.7 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V7a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  },
];

// "Chrome" aplikasi: state navigasi sidebar + label header/footer.
//
// Modul ini HANYA menangani presentasi shell (tidak ada state domain task/
// project). Ketergantungan pada data domain (runtime/queue/projects/config)
// diberikan sebagai GETTER agar modul ini tetap murni presentasi dan tidak
// menciptakan ketergantungan melingkar dengan modul lain.
export function useShellState({ getRuntime, getConfig, getQueueCount, getProjectCount }) {
  const activeNav = ref("agent");
  // Extension management refresh key — incremented after operations that need catalog refresh
  const extensionRefreshKey = ref(0);

  // Sidebar: Workspace (agent/tasks/projects) & Configuration (settings).
  const workspaceNav = computed(() => navItems.filter((i) => i.id !== "settings"));
  const settingsItem = computed(() => navItems.find((i) => i.id === "settings") || {});

  function navBadge(id) {
    if (id === "tasks") return getQueueCount() || null;
    if (id === "projects") return getProjectCount() || null;
    return null;
  }

  // Alamat gateway yang DITAMPILKAN di sidebar = origin AKTUAL browser (host +
  // port yang benar-benar dipakai server). Aman saat SSR (Node tanpa `window`).
  const gatewayAddress = computed(() =>
    typeof window !== "undefined" && window.location ? window.location.host : ""
  );

  // Footer status bar (dari runtime AETHER, bukan hardcode).
  const modelLabel = computed(() => getRuntime().model || getConfig().model || "—");
  const providerLabel = computed(() => getRuntime().provider || getConfig().provider || "—");
  // Versi AETHER untuk footer (sumber sama dengan data/version.json).
  const aetherVersion = versionInfo.version;

  // Page header (tasks/projects/settings).
  const pageTitle = computed(() => {
    if (activeNav.value === "tasks") return "Tasks";
    if (activeNav.value === "projects") return "Projects";
    if (activeNav.value === "backup") return "Backup";
    if (activeNav.value === "extension") return "Extension";
    if (activeNav.value === "settings") return "Settings";
    return "Workbench";
  });
  const pageDesc = computed(() => {
    if (activeNav.value === "tasks") return "Live task queue and past task history.";
    if (activeNav.value === "projects") return "Workspaces registered in AETHER.";
    if (activeNav.value === "backup") return "GitHub backup, checkpoints, and recovery for the active project.";
    if (activeNav.value === "extension") return "Manage AETHER extensions.";
    if (activeNav.value === "settings") return "Configure providers and models used by the AETHER workbench.";
    return "";
  });

  return {
    activeNav,
    extensionRefreshKey,
    workspaceNav,
    settingsItem,
    navBadge,
    gatewayAddress,
    modelLabel,
    providerLabel,
    aetherVersion,
    pageTitle,
    pageDesc,
  };
}
