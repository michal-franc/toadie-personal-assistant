package com.claudewatch.app.network

import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)

/**
 * Tests for WatchWebSocketClient debounce and dedup logic.
 *
 * Since WatchWebSocketClient is tightly coupled to Android (RelayClient, Log),
 * we test the extracted logic: the connection status state machine with grace
 * period, and history message deduplication.
 */
class WatchWebSocketClientTest {

    // --- Connection status debounce state machine ---
    // Mirrors the logic in WatchWebSocketClient's onWebSocketStatus callback

    enum class ConnectionStatus { DISCONNECTED, CONNECTING, CONNECTED }

    class ConnectionStatusDebouncer(
        private val scope: CoroutineScope,
        private val graceMs: Long = 2000L
    ) {
        var status = ConnectionStatus.DISCONNECTED
            private set
        var disconnectGraceJob: Job? = null
            private set
        var reconnectRequested = false
            private set

        fun onStatus(relayStatus: String) {
            when (relayStatus) {
                "connected" -> {
                    disconnectGraceJob?.cancel()
                    status = ConnectionStatus.CONNECTED
                }
                "connecting" -> {
                    if (status != ConnectionStatus.CONNECTED) {
                        disconnectGraceJob?.cancel()
                        status = ConnectionStatus.CONNECTING
                    }
                }
                "disconnected" -> {
                    if (status != ConnectionStatus.DISCONNECTED) {
                        disconnectGraceJob?.cancel()
                        disconnectGraceJob = scope.launch {
                            delay(graceMs)
                            status = ConnectionStatus.DISCONNECTED
                            reconnectRequested = true
                        }
                    } else {
                        reconnectRequested = true
                    }
                }
            }
        }

        fun resetReconnectFlag() {
            reconnectRequested = false
        }
    }

    // --- Debounce tests ---

    @Test
    fun `connected status is applied immediately`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this)
        debouncer.onStatus("connected")
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
    }

    @Test
    fun `connecting is applied when not connected`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this)
        assertEquals(ConnectionStatus.DISCONNECTED, debouncer.status)
        debouncer.onStatus("connecting")
        assertEquals(ConnectionStatus.CONNECTING, debouncer.status)
    }

    @Test
    fun `connecting is ignored when already connected`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this)
        debouncer.onStatus("connected")
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)

        debouncer.onStatus("connecting")
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
    }

    @Test
    fun `disconnected starts grace period, not immediate`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        debouncer.onStatus("disconnected")
        // Status should still be CONNECTED during grace period
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
        assertNotNull(debouncer.disconnectGraceJob)
    }

    @Test
    fun `disconnected fires after grace period`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        debouncer.onStatus("disconnected")
        advanceTimeBy(2001L)

        assertEquals(ConnectionStatus.DISCONNECTED, debouncer.status)
        assertTrue(debouncer.reconnectRequested)
    }

    @Test
    fun `connected cancels grace period`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        debouncer.onStatus("disconnected")
        // Recover before grace fires
        advanceTimeBy(500L)
        debouncer.onStatus("connected")

        advanceTimeBy(2000L)
        // Should still be connected, grace was cancelled
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
        assertFalse(debouncer.reconnectRequested)
    }

    @Test
    fun `rapid connecting-disconnected-connected cycle stays connected`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        // Simulate the rapid relay cycle observed in logs
        debouncer.onStatus("connecting")   // ignored (already connected)
        debouncer.onStatus("disconnected") // starts grace
        debouncer.onStatus("connected")    // cancels grace

        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)

        advanceTimeBy(3000L)
        // Still connected after grace period would have fired
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
        assertFalse(debouncer.reconnectRequested)
    }

    @Test
    fun `multiple rapid cycles stay connected`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        // 5 cycles like the real relay does every 5 seconds
        repeat(5) {
            debouncer.onStatus("connecting")
            debouncer.onStatus("disconnected")
            debouncer.onStatus("connected")
        }

        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
        advanceTimeBy(5000L)
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)
        assertFalse(debouncer.reconnectRequested)
    }

    @Test
    fun `disconnected when already disconnected triggers reconnect immediately`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        assertEquals(ConnectionStatus.DISCONNECTED, debouncer.status)

        debouncer.onStatus("disconnected")
        assertTrue(debouncer.reconnectRequested)
    }

    @Test
    fun `grace period resets on new disconnected`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        debouncer.onStatus("connected")

        debouncer.onStatus("disconnected")
        advanceTimeBy(1500L) // 500ms before grace fires
        // New disconnect resets the timer
        debouncer.onStatus("disconnected")
        advanceTimeBy(1500L) // 1500ms into new grace — not fired yet
        assertEquals(ConnectionStatus.CONNECTED, debouncer.status)

        advanceTimeBy(600L) // Now past the new grace
        assertEquals(ConnectionStatus.DISCONNECTED, debouncer.status)
    }

    @Test
    fun `connecting from disconnected then disconnected uses grace`() = runTest {
        val debouncer = ConnectionStatusDebouncer(this, graceMs = 2000L)
        // Start disconnected, then go through connecting
        debouncer.onStatus("connecting")
        assertEquals(ConnectionStatus.CONNECTING, debouncer.status)

        debouncer.onStatus("disconnected")
        // CONNECTING != DISCONNECTED, so grace period should start
        assertEquals(ConnectionStatus.CONNECTING, debouncer.status)
        advanceTimeBy(2001L)
        assertEquals(ConnectionStatus.DISCONNECTED, debouncer.status)
    }

    // --- History dedup tests ---

    data class SimpleMessage(val role: String, val content: String, val timestamp: String)

    private fun messagesMatch(
        newMessages: List<SimpleMessage>,
        oldMessages: List<SimpleMessage>
    ): Boolean {
        return newMessages.size == oldMessages.size &&
            newMessages.zip(oldMessages).all { (a, b) ->
                a.role == b.role && a.content == b.content && a.timestamp == b.timestamp
            }
    }

    @Test
    fun `identical history messages are detected as same`() {
        val messages = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi there", "t2")
        )
        assertTrue(messagesMatch(messages, messages))
    }

    @Test
    fun `same content different instances are detected as same`() {
        val old = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi", "t2")
        )
        val new = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi", "t2")
        )
        assertTrue(messagesMatch(new, old))
    }

    @Test
    fun `different content is detected as different`() {
        val old = listOf(SimpleMessage("user", "Hello", "t1"))
        val new = listOf(SimpleMessage("user", "Goodbye", "t1"))
        assertFalse(messagesMatch(new, old))
    }

    @Test
    fun `different size is detected as different`() {
        val old = listOf(SimpleMessage("user", "Hello", "t1"))
        val new = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi", "t2")
        )
        assertFalse(messagesMatch(new, old))
    }

    @Test
    fun `empty histories match`() {
        assertTrue(messagesMatch(emptyList(), emptyList()))
    }

    @Test
    fun `new message appended is detected as different`() {
        val old = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi", "t2")
        )
        val new = listOf(
            SimpleMessage("user", "Hello", "t1"),
            SimpleMessage("claude", "Hi", "t2"),
            SimpleMessage("user", "How are you?", "t3")
        )
        assertFalse(messagesMatch(new, old))
    }
}
