import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Svelte 5. vitePreprocess enables <script lang="ts"> in components; svelte-check reads this config.
export default {
  preprocess: vitePreprocess(),
};
