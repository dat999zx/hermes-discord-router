describe('router cross-process tile refresh regression', () => {
  it.each([false, true])('reads the stored tail while sidebar publication is delayed (messaging=%s)', async messaging => {
    const storedId = 'stored-router-foreign'
    const runtimeId = 'runtime-router-foreign'
    const row = makeSessionInfo({ id: storedId, last_active: 1, message_count: 2, preview: 'old' })
    const publishRows = messaging ? setMessagingSessions : setSessions
    publishRows([row])
    $changeEventsAvailable.set(true)
    $activeSessionId.set(ACTIVE_RUNTIME_ID)
    $selectedStoredSessionId.set(ACTIVE_STORED_ID)
    $sessionTiles.set([{ storedSessionId: storedId, runtimeId }])
    let tileState = createClientSessionState(storedId)
    publishSessionState(runtimeId, tileState)
    let persisted = 'old'
    vi.mocked(getLatestSessionMessages).mockImplementation(async id => transcript(persisted, id) as never)
    let delayed = false
    let releaseList: () => void = () => undefined
    const refreshList = vi.fn(() => delayed ? new Promise<void>(resolve => {
      releaseList = () => {
        publishRows([{ ...row, last_active: 2, message_count: 4, preview: 'new foreign answer' }])
        resolve()
      }
    }) : Promise.resolve())
    const noop = vi.fn(async () => undefined)
    const request = vi.fn(async () => ({ sessions: [] }))
    const update: Parameters<typeof useBackgroundSync>[0]['updateSessionState'] = (_id, updater) => {
      tileState = updater(tileState)
      publishSessionState(runtimeId, tileState)
      return tileState
    }
    const params = {
      activeConnectionId: 'local', activeGatewayProfile: 'default', activeIsMessaging: false,
      activeSessionId: ACTIVE_RUNTIME_ID, activeStoredSessionId: ACTIVE_STORED_ID,
      freshDraftReady: false, gatewayState: 'open' as const,
      refreshActiveTranscript: noop, refreshCronJobs: noop, refreshCurrentModel: noop,
      refreshHermesConfig: noop, refreshMessagingSessions: messaging ? refreshList : noop,
      refreshSessions: messaging ? noop : refreshList, requestGateway: request as never,
      updateSessionState: update
    }
    renderHook(() => useBackgroundSync(params))
    let now = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    await act(async () => { notifySessionsChanged() })
    expect(tileState.messages.at(-1)?.parts[0]).toMatchObject({ text: 'old' })
    const previousReads = vi.mocked(getLatestSessionMessages).mock.calls.length
    delayed = true
    persisted = 'new foreign answer'
    now += 10001
    await act(async () => { notifySessionsChanged() })
    await act(async () => { releaseList() })
    expect(getLatestSessionMessages).toHaveBeenCalledTimes(previousReads + 1)
    expect(tileState.messages.at(-1)?.parts[0]).toMatchObject({ text: 'new foreign answer' })
  })
})
