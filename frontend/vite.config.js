import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = process.env.BACKEND_PORT || '8006'
const PORT = Number(process.env.FRONTEND_PORT || 5180)

// TC_LAN=1 binds to every interface so a phone on the same wifi can reach the
// app. It is OFF by default and deliberately so: the backend it proxies to holds
// Robinhood credentials and can place orders, so making it reachable is a
// decision, not a default. With it on, the backend requires a token for anything
// that is not loopback (see backend/app/core/access.py).
const on = (v) => ['1', 'true', 'yes', 'on'].includes(String(v || '').toLowerCase())
const LAN = on(process.env.TC_LAN)
const TUNNEL = on(process.env.TC_TUNNEL)
// Either way the dev server must answer a Host header it did not choose.
const OPEN = LAN || TUNNEL

export default defineConfig({
  plugins: [react()],
  server: {
    host: LAN ? '0.0.0.0' : '127.0.0.1',
    port: PORT,
    strictPort: true,
    // The phone reaches vite by IP, and vite rejects unknown Host headers by
    // default — which shows up as a blank page with no obvious cause.
    allowedHosts: OPEN ? true : undefined,
    // xfwd adds X-Forwarded-For. Without it the backend sees every request as
    // coming from 127.0.0.1 -- including requests that arrived through a public
    // tunnel -- and the loopback exemption in core/access.py would let the whole
    // internet past the token. This one flag is what makes the token mean
    // anything once the app is reachable from outside.
    proxy: { '/api': { target: `http://127.0.0.1:${BACKEND}`, changeOrigin: true, xfwd: true } },
  },
  preview: {
    host: LAN ? '0.0.0.0' : '127.0.0.1',
    port: PORT,
    allowedHosts: OPEN ? true : undefined,
    // xfwd adds X-Forwarded-For. Without it the backend sees every request as
    // coming from 127.0.0.1 -- including requests that arrived through a public
    // tunnel -- and the loopback exemption in core/access.py would let the whole
    // internet past the token. This one flag is what makes the token mean
    // anything once the app is reachable from outside.
    proxy: { '/api': { target: `http://127.0.0.1:${BACKEND}`, changeOrigin: true, xfwd: true } },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // FIXED FILENAMES, because frontend/dist is COMMITTED (it is what lets
    // someone with no Node run this at all). With Vite's content hashes every
    // rebuild writes index-<newhash>.js and leaves index-<oldhash>.js behind in
    // git forever, so the repo grows a little graveyard on each release and
    // nothing ever removes it.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/app.[ext]',
      },
    },
  },
})
