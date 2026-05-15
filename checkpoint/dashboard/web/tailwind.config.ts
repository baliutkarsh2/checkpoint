import type { Config } from "tailwindcss";

// Design tokens lifted directly from the existing dashboard CSS so the SPA
// matches usecheckpoint.dev exactly. Anything not in this file should be
// expressible via Tailwind utility classes — no custom CSS in component files.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        paper: {
          DEFAULT: "#f5f2ea",
          2: "#ebe7dc",
          3: "#e0dccf",
        },
        ink: {
          DEFAULT: "#0a0a0a",
          2: "#1f1d18",
          3: "#5a5648",
          4: "#8a8472",
          5: "#b8b2a0",
        },
        accent: { DEFAULT: "#2dff5c", soft: "#5fff7a" },
        pass: { DEFAULT: "#0ea83b", soft: "#d6f5dd" },
        fail: { DEFAULT: "#d73838", soft: "#f8dada" },
        warn: { DEFAULT: "#c89124", soft: "#f9ecd0" },
        info: { DEFAULT: "#2a5fb8", soft: "#d6e3f4" },
        judge: { DEFAULT: "#7a4ec6", soft: "#e6dbf3" },
      },
      fontFamily: {
        sans: ["Geist", "system-ui", "-apple-system", "sans-serif"],
        mono: ["Geist Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        micro: ["11px", { lineHeight: "1.4", letterSpacing: "0.01em" }],
        label: ["11px", { lineHeight: "1.4", letterSpacing: "0.08em" }],
      },
      boxShadow: {
        offset: "6px 6px 0 0 #0a0a0a",
        "offset-sm": "3px 3px 0 0 #0a0a0a",
        "offset-accent": "6px 6px 0 0 #2dff5c",
      },
      borderRadius: { none: "0px" },
      animation: {
        blip: "blip 1.5s ease-in-out infinite",
      },
      keyframes: {
        blip: {
          "0%, 100%": {
            opacity: "1",
            boxShadow: "0 0 0 0 rgba(45, 255, 92, 0.5)",
          },
          "50%": {
            opacity: "0.6",
            boxShadow: "0 0 0 6px rgba(45, 255, 92, 0)",
          },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
