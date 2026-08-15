'use strict';

const {
  FORBIDDEN_CHROMIUM_SWITCHES,
  hardenElectronBootstrap,
} = require('./bootstrap-hardening.cjs');

try {
  hardenElectronBootstrap();
  // NODE_PATH is expanded into Module.globalPaths before user code starts.
  // Recompute the cache after sanitization so deleting the variable is effective.
  const NodeModule = require('node:module');
  if (typeof NodeModule._initPaths !== 'function') {
    throw new Error('Node module search-path reset is unavailable');
  }
  NodeModule._initPaths();
  // Environment/argv are clean before Electron is imported. Remove Chromium's
  // internal copies before any application module can create WebContents.
  const { app } = require('electron');
  const removedSwitches = FORBIDDEN_CHROMIUM_SWITCHES.filter(
    (name) => app.commandLine.hasSwitch(name),
  );
  for (const name of FORBIDDEN_CHROMIUM_SWITCHES) {
    app.commandLine.removeSwitch(name);
  }
  const remainingSwitches = FORBIDDEN_CHROMIUM_SWITCHES.filter(
    (name) => app.commandLine.hasSwitch(name),
  );
  if (remainingSwitches.length) {
    throw new Error(
      `failed to remove unsafe Chromium switches: ${remainingSwitches.join(',')}`,
    );
  }
  if (removedSwitches.length) {
    process.stderr.write(
      `[electron-hardening] removed Chromium switches: ${removedSwitches.join(',')}\n`,
    );
  }
  app.enableSandbox();
} catch (error) {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  process.stderr.write(`[electron-hardening] FAIL-CLOSED ${detail}\n`);
  throw error;
}

require('./index.js');
