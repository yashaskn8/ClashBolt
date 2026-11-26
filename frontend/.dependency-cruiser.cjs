module.exports = {
  forbidden: [
    {
      name: 'no-circular-imports',
      severity: 'error',
      from: {},
      to: { circular: true },
    },
    {
      name: 'no-unresolved-imports',
      severity: 'error',
      from: {},
      to: { couldNotResolve: true },
    },
    {
      name: 'no-import-of-application-entry-points',
      severity: 'error',
      from: { path: '^src/(?!main\\.tsx$)' },
      to: { path: '^src/(App|main)\\.tsx$' },
    },
    {
      name: 'no-test-dependencies-in-application',
      severity: 'error',
      from: { path: '^src/' },
      to: { path: '^e2e/' },
    },
    {
      name: 'no-dev-packages-in-application',
      severity: 'error',
      from: { path: '^src/' },
      to: { dependencyTypes: ['npm-dev'] },
    },
    {
      name: 'browser-tests-use-public-interfaces',
      severity: 'error',
      from: { path: '^e2e/' },
      to: { path: '^src/' },
    },
  ],
  options: {
    doNotFollow: { path: 'node_modules' },
    tsConfig: { fileName: 'tsconfig.json' },
    tsPreCompilationDeps: true,
  },
}
