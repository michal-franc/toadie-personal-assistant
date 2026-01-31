package com.claudewatch.companion.relay

import android.content.Context
import android.util.Log
import com.claudewatch.companion.SettingsActivity
import com.google.android.gms.wearable.Wearable
import kotlinx.coroutines.*
import kotlinx.coroutines.tasks.await
import okhttp3.*
import java.util.concurrent.TimeUnit

/**
 * Singleton that manages a WebSocket connection to the server on behalf of the watch.
 * Forwards all server messages to the watch via DataLayer MessageClient.
 *
 * Must be a singleton because WearableListenerService instances are short-lived —
 * Android creates and destroys them per message. The WebSocket must outlive the service.
 */
object RelayWebSocketManager {

    private const val TAG = "RelayWSManager"
    private const val RECONNECT_DELAY_MS = 5000L
    const val PATH_WS_MESSAGE = "/relay/ws/message"
    const val PATH_WS_STATUS = "/relay/ws/status"

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private var context: Context? = null
    private var webSocket: WebSocket? = null
    private var reconnectJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var watchNodeId: String? = null
    var isConnected = false
        private set

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (webSocket !== this@RelayWebSocketManager.webSocket) return
            Log.i(TAG, "Watch-relay WebSocket connected")
            isConnected = true
            sendStatusToWatch("connected")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (webSocket !== this@RelayWebSocketManager.webSocket) return
            Log.d(TAG, "Relay WS received: $text")
            forwardToWatch(text)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "Relay WS closing: $code $reason")
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (webSocket !== this@RelayWebSocketManager.webSocket) return
            Log.i(TAG, "Relay WS closed: $code $reason")
            isConnected = false
            sendStatusToWatch("disconnected")
            scheduleReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (webSocket !== this@RelayWebSocketManager.webSocket) return
            Log.e(TAG, "Relay WS error", t)
            isConnected = false
            sendStatusToWatch("disconnected")
            scheduleReconnect()
        }
    }

    fun connect(ctx: Context, fromNodeId: String) {
        context = ctx.applicationContext
        watchNodeId = fromNodeId
        reconnectJob?.cancel()

        val serverAddress = SettingsActivity.getServerAddress(ctx)
        val deviceId = "watch-relay"
        val wsUrl = "ws://$serverAddress/ws?device=watch&id=${java.net.URLEncoder.encode(deviceId, "UTF-8")}"
        Log.i(TAG, "Opening relay WebSocket: $wsUrl")

        sendStatusToWatch("connecting")

        val request = Request.Builder()
            .url(wsUrl)
            .build()

        webSocket?.close(1000, "Reconnecting")
        webSocket = client.newWebSocket(request, listener)
    }

    fun disconnect() {
        reconnectJob?.cancel()
        webSocket?.close(1000, "Watch disconnect")
        webSocket = null
        isConnected = false
    }

    private fun scheduleReconnect() {
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            delay(RECONNECT_DELAY_MS)
            val ctx = context ?: return@launch
            val nodeId = watchNodeId ?: return@launch
            connect(ctx, nodeId)
        }
    }

    private fun forwardToWatch(text: String) {
        val ctx = context ?: return
        val nodeId = watchNodeId ?: return
        scope.launch {
            try {
                Wearable.getMessageClient(ctx)
                    .sendMessage(nodeId, PATH_WS_MESSAGE, text.toByteArray())
                    .await()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to forward WS message to watch", e)
            }
        }
    }

    private fun sendStatusToWatch(status: String) {
        val ctx = context ?: return
        val nodeId = watchNodeId ?: return
        scope.launch {
            try {
                Wearable.getMessageClient(ctx)
                    .sendMessage(nodeId, PATH_WS_STATUS, status.toByteArray())
                    .await()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send WS status to watch", e)
            }
        }
    }
}
