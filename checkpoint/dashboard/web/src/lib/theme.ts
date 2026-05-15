import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

export function useTheme(): [Theme, (t: Theme) => void, () => void] {
  const [theme, setThemeState] = useState<Theme>(
    () =>
      (typeof localStorage !== "undefined" &&
        (localStorage.getItem("ck.theme") as Theme | null)) ||
      "light",
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("ck.theme", theme);
  }, [theme]);

  const setTheme = (t: Theme) => setThemeState(t);
  const toggle = () => setThemeState((t) => (t === "light" ? "dark" : "light"));
  return [theme, setTheme, toggle];
}
