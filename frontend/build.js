const esbuild = require("esbuild");
const fs = require("fs");
const path = require("path");

const outdir = path.resolve(__dirname, "../delegate/static");
const watch = process.argv.includes("--watch");

async function build() {
  fs.mkdirSync(outdir, { recursive: true });

  // Copy index.html to output with cache-busting query params
  const cacheBust = Date.now();
  let html = fs.readFileSync(path.join(__dirname, "index.html"), "utf-8");
  html = html.replace('/static/app.js"', `/static/app.js?v=${cacheBust}"`);
  html = html.replace('/static/styles.css"', `/static/styles.css?v=${cacheBust}"`);
  fs.writeFileSync(path.join(outdir, "index.html"), html);

  // Copy public/ assets (favicon, icons, etc.)
  const publicDir = path.join(__dirname, "public");
  if (fs.existsSync(publicDir)) {
    for (const file of fs.readdirSync(publicDir)) {
      fs.copyFileSync(
        path.join(publicDir, file),
        path.join(outdir, file)
      );
    }
  }

  const ctx = await esbuild.context({
    entryPoints: [
      path.join(__dirname, "src/app.jsx"),
      path.join(__dirname, "src/styles.css"),
    ],
    bundle: true,
    outdir,
    minify: !watch,
    sourcemap: watch,
    format: "iife",
    target: ["es2020"],
    jsx: "automatic",
    jsxImportSource: "preact",
    loader: {
      ".woff": "file",
      ".woff2": "file",
    },
  });

  if (watch) {
    await ctx.watch();
    // Also watch index.html and public/ — copy on change
    const htmlPath = path.join(__dirname, "index.html");
    fs.watchFile(htmlPath, { interval: 300 }, () => {
      const bust = Date.now();
      let h = fs.readFileSync(htmlPath, "utf-8");
      h = h.replace('/static/app.js"', `/static/app.js?v=${bust}"`);
      h = h.replace('/static/styles.css"', `/static/styles.css?v=${bust}"`);
      fs.writeFileSync(path.join(outdir, "index.html"), h);
      console.log("Copied index.html");
    });
    if (fs.existsSync(publicDir)) {
      for (const file of fs.readdirSync(publicDir)) {
        const src = path.join(publicDir, file);
        fs.watchFile(src, { interval: 300 }, () => {
          fs.copyFileSync(src, path.join(outdir, file));
          console.log(`Copied public/${file}`);
        });
      }
    }
    console.log("Watching for changes...");
  } else {
    await ctx.rebuild();
    await ctx.dispose();
    console.log("Build complete →", outdir);
  }
}

build().catch((e) => {
  console.error(e);
  process.exit(1);
});
