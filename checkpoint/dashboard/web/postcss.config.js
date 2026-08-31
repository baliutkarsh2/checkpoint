// Tailwind CSS v4 ships its PostCSS integration as a separate plugin and does
// its own vendor-prefixing (via Lightning CSS), so autoprefixer is no longer
// needed here.
export default {
  plugins: { "@tailwindcss/postcss": {} },
};
