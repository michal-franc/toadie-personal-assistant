package com.claudewatch.companion.relay

import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test

/**
 * Tests for RelayWebSocketManager's stale callback detection.
 *
 * The relay replaces the WebSocket on reconnect (close old, create new).
 * Callbacks from the OLD WebSocket must be ignored — otherwise onClosed()
 * from the replaced WS triggers scheduleReconnect(), creating a 5-second
 * reconnect cycle even when the connection is healthy.
 */
class RelayWebSocketManagerTest {

    /**
     * Simulates the relay's WebSocket identity check pattern.
     * Mirrors RelayWebSocketManager's `if (webSocket !== this.webSocket) return` guard.
     */
    class RelaySimulator(private val scope: CoroutineScope) {
        var currentWs: Any? = null
            private set
        var isConnected = false
            private set
        var reconnectScheduled = false
            private set
        val statusesSent = mutableListOf<String>()

        fun connect(): Any {
            reconnectScheduled = false
            val oldWs = currentWs
            val newWs = Object() // new WebSocket instance
            currentWs = newWs
            statusesSent.add("connecting")
            // In real code: oldWs?.close() would trigger onClosed on the OLD ws
            return newWs
        }

        fun disconnect() {
            currentWs = null
            isConnected = false
        }

        // Simulates WebSocketListener.onOpen
        fun onOpen(ws: Any) {
            if (ws !== currentWs) return // stale check
            isConnected = true
            statusesSent.add("connected")
        }

        // Simulates WebSocketListener.onClosed
        fun onClosed(ws: Any) {
            if (ws !== currentWs) return // stale check
            isConnected = false
            statusesSent.add("disconnected")
            scheduleReconnect()
        }

        // Simulates WebSocketListener.onFailure
        fun onFailure(ws: Any) {
            if (ws !== currentWs) return // stale check
            isConnected = false
            statusesSent.add("disconnected")
            scheduleReconnect()
        }

        // Simulates WebSocketListener.onMessage
        var lastMessage: String? = null
            private set

        fun onMessage(ws: Any, text: String) {
            if (ws !== currentWs) return // stale check
            lastMessage = text
        }

        private fun scheduleReconnect() {
            reconnectScheduled = true
        }
    }

    @Test
    fun `onOpen from current WebSocket sets connected`() = runTest {
        val relay = RelaySimulator(this)
        val ws = relay.connect()
        relay.onOpen(ws)

        assertTrue(relay.isConnected)
        assertTrue(relay.statusesSent.contains("connected"))
    }

    @Test
    fun `onOpen from stale WebSocket is ignored`() = runTest {
        val relay = RelaySimulator(this)
        val oldWs = relay.connect()
        relay.connect() // replaces oldWs

        relay.onOpen(oldWs) // stale callback
        assertFalse(relay.isConnected) // should not have changed
        assertEquals(listOf("connecting", "connecting"), relay.statusesSent)
    }

    @Test
    fun `onClosed from stale WebSocket does not trigger reconnect`() = runTest {
        val relay = RelaySimulator(this)
        val oldWs = relay.connect()
        relay.onOpen(oldWs)
        assertTrue(relay.isConnected)

        // Reconnect replaces the WebSocket
        val newWs = relay.connect()
        relay.onOpen(newWs)
        assertTrue(relay.isConnected)

        // Old WS closes (this is what caused the 5-second cycle)
        relay.onClosed(oldWs)

        // Should still be connected, reconnect NOT scheduled
        assertTrue(relay.isConnected)
        assertFalse(relay.reconnectScheduled)
    }

    @Test
    fun `onClosed from current WebSocket triggers reconnect`() = runTest {
        val relay = RelaySimulator(this)
        val ws = relay.connect()
        relay.onOpen(ws)

        relay.onClosed(ws)

        assertFalse(relay.isConnected)
        assertTrue(relay.reconnectScheduled)
        assertTrue(relay.statusesSent.contains("disconnected"))
    }

    @Test
    fun `onFailure from stale WebSocket is ignored`() = runTest {
        val relay = RelaySimulator(this)
        val oldWs = relay.connect()
        relay.onOpen(oldWs)

        val newWs = relay.connect()
        relay.onOpen(newWs)

        relay.onFailure(oldWs)
        assertTrue(relay.isConnected)
        assertFalse(relay.reconnectScheduled)
    }

    @Test
    fun `onFailure from current WebSocket triggers reconnect`() = runTest {
        val relay = RelaySimulator(this)
        val ws = relay.connect()
        relay.onOpen(ws)

        relay.onFailure(ws)

        assertFalse(relay.isConnected)
        assertTrue(relay.reconnectScheduled)
    }

    @Test
    fun `onMessage from stale WebSocket is ignored`() = runTest {
        val relay = RelaySimulator(this)
        val oldWs = relay.connect()
        relay.onOpen(oldWs)

        val newWs = relay.connect()
        relay.onOpen(newWs)

        relay.onMessage(oldWs, "stale message")
        assertNull(relay.lastMessage)

        relay.onMessage(newWs, "current message")
        assertEquals("current message", relay.lastMessage)
    }

    @Test
    fun `reconnect cycle simulation - old close does not cascade`() = runTest {
        val relay = RelaySimulator(this)

        // Initial connect
        val ws1 = relay.connect()
        relay.onOpen(ws1)
        assertTrue(relay.isConnected)

        // Simulate reconnect (e.g., from external trigger)
        val ws2 = relay.connect()
        // ws1 closes asynchronously
        relay.onClosed(ws1)  // MUST be ignored
        // ws2 opens
        relay.onOpen(ws2)

        assertTrue(relay.isConnected)
        assertFalse(relay.reconnectScheduled)

        // Another reconnect cycle
        val ws3 = relay.connect()
        relay.onClosed(ws2)  // MUST be ignored
        relay.onOpen(ws3)

        assertTrue(relay.isConnected)
        assertFalse(relay.reconnectScheduled)
    }

    @Test
    fun `disconnect nulls websocket so all callbacks are ignored`() = runTest {
        val relay = RelaySimulator(this)
        val ws = relay.connect()
        relay.onOpen(ws)

        relay.disconnect()
        assertFalse(relay.isConnected)

        // Late callback from disconnected WS
        relay.onClosed(ws)
        assertFalse(relay.reconnectScheduled) // currentWs is null, ws !== null
    }

    @Test
    fun `connect cancels pending reconnect`() = runTest {
        val relay = RelaySimulator(this)
        val ws1 = relay.connect()
        relay.onOpen(ws1)
        relay.onClosed(ws1) // triggers reconnectScheduled
        assertTrue(relay.reconnectScheduled)

        // New connect should reset the flag
        relay.connect()
        assertFalse(relay.reconnectScheduled)
    }
}
