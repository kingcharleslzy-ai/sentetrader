// Root ESLint flat config. `npm run lint` covers the TypeScript client and the
// shared ui-events package; Rust, Python and build scripts have their own checks.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/dist-web/**",
      "**/dev-dist/**",
      "**/playwright-report/**",
      "**/test-results/**",
      "apps/desktop/**",
      "apps/client/src/workbench.generated.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" },
      ],
      // Baseline exemptions from the first static-check PR. The React Compiler
      // rules below are re-enabled when App.tsx / FuturesDecisionWorkspace.tsx
      // are split (refactor plan 3.2); `any` is tolerated until then as well.
      "@typescript-eslint/no-explicit-any": "off",
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/refs": "off",
      "react-hooks/immutability": "off",
      "react-hooks/purity": "off",
      "react-hooks/preserve-manual-memoization": "off",
    },
  },
);
