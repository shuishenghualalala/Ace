/// <reference types="vite/client" />

interface Window {
  Crew?: {
    openPath: (path: string) => Promise<string>;
  };
}
