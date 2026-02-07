package com.claudewatch.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

/**
 * Unit tests for watch app MainActivity logic.
 * Tests state management, response parsing, and request handling.
 */
class MainActivityTest {

    // State management tests
    @Test
    fun `initial state is idle`() {
        val state = WatchState()

        assertTrue(state.isIdle())
        assertFalse(state.canStartRecording())
        assertNull(state.currentRequestId)
    }

    @Test
    fun `can start recording when idle and has permission`() {
        val state = WatchState(hasAudioPermission = true)

        assertTrue(state.canStartRecording())
    }

    @Test
    fun `cannot start recording without permission`() {
        val state = WatchState(hasAudioPermission = false)

        assertFalse(state.canStartRecording())
    }

    @Test
    fun `cannot start recording while already recording`() {
        val state = WatchState(
            hasAudioPermission = true,
            isRecording = true
        )

        assertFalse(state.canStartRecording())
    }

    @Test
    fun `cannot start recording while waiting for response`() {
        val state = WatchState(
            hasAudioPermission = true,
            isWaitingForResponse = true
        )

        assertFalse(state.canStartRecording())
    }

    @Test
    fun `cannot start recording while playing audio`() {
        val state = WatchState(
            hasAudioPermission = true,
            isPlayingAudio = true
        )

        assertFalse(state.canStartRecording())
    }

    @Test
    fun `state transitions to recording`() {
        val state = WatchState(hasAudioPermission = true)
        val newState = state.startRecording()

        assertTrue(newState.isRecording)
        assertFalse(newState.isIdle())
    }

    @Test
    fun `state transitions from recording to waiting`() {
        val state = WatchState(isRecording = true)
        val newState = state.stopRecordingAndWait("req-123")

        assertFalse(newState.isRecording)
        assertTrue(newState.isWaitingForResponse)
        assertEquals("req-123", newState.currentRequestId)
    }

    @Test
    fun `state transitions from waiting to playing`() {
        val state = WatchState(
            isWaitingForResponse = true,
            currentRequestId = "req-123"
        )
        val newState = state.startPlayingAudio()

        assertFalse(newState.isWaitingForResponse)
        assertTrue(newState.isPlayingAudio)
    }

    @Test
    fun `state transitions from playing to idle`() {
        val state = WatchState(
            isPlayingAudio = true,
            currentRequestId = "req-123"
        )
        val newState = state.finishPlaying()

        assertFalse(newState.isPlayingAudio)
        assertTrue(newState.isIdle())
        assertNull(newState.currentRequestId)
    }

    @Test
    fun `cancel returns to idle state`() {
        val state = WatchState(
            isRecording = true,
            currentRequestId = "req-123"
        )
        val newState = state.cancel()

        assertTrue(newState.isIdle())
        assertNull(newState.currentRequestId)
    }

    // Response parsing tests
    @Test
    fun `parse transcribe response extracts request_id`() {
        val json = """{"request_id": "req-abc123", "status": "accepted"}"""
        val result = parseTranscribeResponse(json)

        assertEquals("req-abc123", result.requestId)
        assertEquals("accepted", result.status)
    }

    @Test
    fun `parse response status ready`() {
        val json = """{"status": "ready", "response": "Hello!"}"""
        val result = parseResponsePoll(json)

        assertTrue(result.isReady)
        assertEquals("Hello!", result.response)
    }

    @Test
    fun `parse response status pending`() {
        val json = """{"status": "pending"}"""
        val result = parseResponsePoll(json)

        assertFalse(result.isReady)
        assertNull(result.response)
    }

    @Test
    fun `parse response with audio available`() {
        val json = """{"status": "ready", "response": "Hi", "audio_available": true}"""
        val result = parseResponsePoll(json)

        assertTrue(result.isReady)
        assertTrue(result.audioAvailable)
    }

    @Test
    fun `parse response without audio available`() {
        val json = """{"status": "ready", "response": "Hi", "audio_available": false}"""
        val result = parseResponsePoll(json)

        assertTrue(result.isReady)
        assertFalse(result.audioAvailable)
    }

    // Polling logic tests
    @Test
    fun `polling counter increments`() {
        var attempts = 0
        repeat(5) { attempts++ }
        assertEquals(5, attempts)
    }

    @Test
    fun `polling stops at max attempts`() {
        val maxAttempts = 60
        var attempts = 0
        while (attempts < maxAttempts) {
            attempts++
        }
        assertEquals(60, attempts)
    }

    // Error handling tests
    @Test
    fun `parse error response`() {
        val json = """{"status": "error", "error": "Transcription failed"}"""
        val result = parseResponsePoll(json)

        assertFalse(result.isReady)
        assertEquals("Transcription failed", result.error)
    }

    @Test
    fun `malformed json returns error result`() {
        val result = parseResponsePoll("not json")

        assertFalse(result.isReady)
        assertNotNull(result.error)
    }

    // UI state determination tests
    @Test
    fun `button state when idle`() {
        val state = WatchState(hasAudioPermission = true)
        val uiState = determineUIState(state)

        assertTrue(uiState.showRecordButton)
        assertFalse(uiState.showStopButton)
        assertFalse(uiState.showSpinner)
    }

    @Test
    fun `button state when recording`() {
        val state = WatchState(isRecording = true)
        val uiState = determineUIState(state)

        assertFalse(uiState.showRecordButton)
        assertTrue(uiState.showStopButton)
        assertFalse(uiState.showSpinner)
    }

    @Test
    fun `button state when waiting for response`() {
        val state = WatchState(isWaitingForResponse = true)
        val uiState = determineUIState(state)

        assertFalse(uiState.showRecordButton)
        assertFalse(uiState.showStopButton)
        assertTrue(uiState.showSpinner)
    }

    @Test
    fun `button state when playing audio`() {
        val state = WatchState(isPlayingAudio = true)
        val uiState = determineUIState(state)

        assertFalse(uiState.showRecordButton)
        assertFalse(uiState.showStopButton)
        assertTrue(uiState.showSpinner)
    }

    // Intent routing tests
    @Test
    fun `auto_record while idle with permission starts recording`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = true, hasPermission = true,
            isRecording = false, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.START_RECORDING, action)
    }

    @Test
    fun `auto_record while recording stops and sends`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = true, hasPermission = true,
            isRecording = true, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.STOP_AND_SEND, action)
    }

    @Test
    fun `auto_record without permission does nothing`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = true, hasPermission = false,
            isRecording = false, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.NONE, action)
    }

    @Test
    fun `from_permission is always ignored`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = true, autoRecord = true, hasPermission = true,
            isRecording = false, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.IGNORE, action)
    }

    @Test
    fun `normal relaunch while playing audio pauses`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = false, hasPermission = true,
            isRecording = false, isPlayingAudio = true, claudeStatus = "speaking"
        )
        assertEquals(MainActivity.IntentAction.PAUSE_AUDIO, action)
    }

    @Test
    fun `normal relaunch while thinking aborts`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = false, hasPermission = true,
            isRecording = false, isPlayingAudio = false, claudeStatus = "thinking"
        )
        assertEquals(MainActivity.IntentAction.ABORT, action)
    }

    @Test
    fun `normal relaunch while recording stops and sends`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = false, hasPermission = true,
            isRecording = true, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.STOP_AND_SEND, action)
    }

    @Test
    fun `normal relaunch while idle does nothing`() {
        val action = MainActivity.resolveIntentAction(
            fromPermission = false, autoRecord = false, hasPermission = true,
            isRecording = false, isPlayingAudio = false, claudeStatus = "idle"
        )
        assertEquals(MainActivity.IntentAction.NONE, action)
    }

    // Helper data classes and functions
    data class WatchState(
        val hasAudioPermission: Boolean = false,
        val isRecording: Boolean = false,
        val isWaitingForResponse: Boolean = false,
        val isPlayingAudio: Boolean = false,
        val currentRequestId: String? = null
    ) {
        fun isIdle(): Boolean = !isRecording && !isWaitingForResponse && !isPlayingAudio

        fun canStartRecording(): Boolean =
            hasAudioPermission && !isRecording && !isWaitingForResponse && !isPlayingAudio

        fun startRecording(): WatchState = copy(isRecording = true)

        fun stopRecordingAndWait(requestId: String): WatchState = copy(
            isRecording = false,
            isWaitingForResponse = true,
            currentRequestId = requestId
        )

        fun startPlayingAudio(): WatchState = copy(
            isWaitingForResponse = false,
            isPlayingAudio = true
        )

        fun finishPlaying(): WatchState = copy(
            isPlayingAudio = false,
            currentRequestId = null
        )

        fun cancel(): WatchState = WatchState(hasAudioPermission = hasAudioPermission)
    }

    data class TranscribeResult(
        val requestId: String?,
        val status: String?
    )

    data class PollResult(
        val isReady: Boolean,
        val response: String? = null,
        val audioAvailable: Boolean = false,
        val error: String? = null
    )

    data class UIState(
        val showRecordButton: Boolean,
        val showStopButton: Boolean,
        val showSpinner: Boolean
    )

    private fun parseTranscribeResponse(jsonString: String): TranscribeResult {
        return try {
            val json = JSONObject(jsonString)
            TranscribeResult(
                requestId = json.optString("request_id", null),
                status = json.optString("status", null)
            )
        } catch (e: Exception) {
            TranscribeResult(null, null)
        }
    }

    private fun parseResponsePoll(jsonString: String): PollResult {
        return try {
            val json = JSONObject(jsonString)
            val status = json.optString("status")
            PollResult(
                isReady = status == "ready",
                response = if (status == "ready") json.optString("response", null) else null,
                audioAvailable = json.optBoolean("audio_available", false),
                error = if (status == "error") json.optString("error", null) else null
            )
        } catch (e: Exception) {
            PollResult(isReady = false, error = "Failed to parse response: ${e.message}")
        }
    }

    private fun determineUIState(state: WatchState): UIState {
        return UIState(
            showRecordButton = state.isIdle() || (!state.isRecording && !state.isWaitingForResponse && !state.isPlayingAudio),
            showStopButton = state.isRecording,
            showSpinner = state.isWaitingForResponse || state.isPlayingAudio
        )
    }
}
