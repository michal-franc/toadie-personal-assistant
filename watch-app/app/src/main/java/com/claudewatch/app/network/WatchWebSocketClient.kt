package com.claudewatch.app.network

import android.util.Log
import com.claudewatch.app.relay.RelayClient
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject

/**
 * WebSocket client for the watch that routes through the phone relay
 * via Wearable DataLayer API instead of direct OkHttp connections.
 */
class WatchWebSocketClient {
    companion object {
        private const val TAG = "WatchWebSocket"
        private const val RECONNECT_DELAY_MS = 5000L
        private const val DISCONNECT_GRACE_MS = 2000L
    }

    private var reconnectJob: Job? = null
    private var disconnectGraceJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // State flows for UI observation
    private val _connectionStatus = MutableStateFlow(ConnectionStatus.DISCONNECTED)
    val connectionStatus: StateFlow<ConnectionStatus> = _connectionStatus

    private val _claudeState = MutableStateFlow(ClaudeState())
    val claudeState: StateFlow<ClaudeState> = _claudeState

    private val _chatMessages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val chatMessages: StateFlow<List<ChatMessage>> = _chatMessages

    private val _currentPrompt = MutableStateFlow<ClaudePrompt?>(null)
    val currentPrompt: StateFlow<ClaudePrompt?> = _currentPrompt

    private val _contextUsage = MutableStateFlow(ContextUsage())
    val contextUsage: StateFlow<ContextUsage> = _contextUsage

    init {
        // Register for relay callbacks
        RelayClient.onWebSocketMessage = { text ->
            Log.d(TAG, "Received via relay: $text")
            handleMessage(text)
        }
        RelayClient.onWebSocketStatus = { status ->
            Log.i(TAG, "WS status via relay: $status")
            when (status) {
                "connected" -> {
                    disconnectGraceJob?.cancel()
                    _connectionStatus.value = ConnectionStatus.CONNECTED
                }
                "connecting" -> {
                    if (_connectionStatus.value != ConnectionStatus.CONNECTED) {
                        disconnectGraceJob?.cancel()
                        _connectionStatus.value = ConnectionStatus.CONNECTING
                    }
                }
                "disconnected" -> {
                    if (_connectionStatus.value != ConnectionStatus.DISCONNECTED) {
                        disconnectGraceJob?.cancel()
                        disconnectGraceJob = scope.launch {
                            delay(DISCONNECT_GRACE_MS)
                            _connectionStatus.value = ConnectionStatus.DISCONNECTED
                            scheduleReconnect()
                        }
                    } else {
                        scheduleReconnect()
                    }
                }
            }
        }
    }

    fun connect() {
        if (_connectionStatus.value == ConnectionStatus.CONNECTING) return

        _connectionStatus.value = ConnectionStatus.CONNECTING
        reconnectJob?.cancel()

        scope.launch {
            try {
                RelayClient.wsConnect()
                Log.i(TAG, "Relay WS connect requested")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to request relay WS connect", e)
                _connectionStatus.value = ConnectionStatus.DISCONNECTED
                scheduleReconnect()
            }
        }
    }

    fun disconnect() {
        reconnectJob?.cancel()
        disconnectGraceJob?.cancel()
        scope.launch {
            try {
                RelayClient.wsDisconnect()
            } catch (e: Exception) {
                Log.e(TAG, "Failed to request relay WS disconnect", e)
            }
        }
        _connectionStatus.value = ConnectionStatus.DISCONNECTED
    }

    fun destroy() {
        disconnectGraceJob?.cancel()
        disconnect()
        RelayClient.onWebSocketMessage = null
        RelayClient.onWebSocketStatus = null
        scope.cancel()
    }

    private fun scheduleReconnect() {
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            delay(RECONNECT_DELAY_MS)
            connect()
        }
    }

    private fun handleMessage(text: String) {
        try {
            val json = JSONObject(text)
            when (json.optString("type")) {
                "state" -> {
                    val requestId = json.optString("request_id", "")
                    _claudeState.value = ClaudeState(
                        status = json.optString("status", "idle"),
                        requestId = requestId.ifEmpty { null }
                    )
                }
                "chat" -> {
                    val message = ChatMessage(
                        role = json.optString("role"),
                        content = json.optString("content"),
                        timestamp = json.optString("timestamp")
                    )
                    _chatMessages.value = _chatMessages.value + message
                }
                "history" -> {
                    val messagesArray = json.optJSONArray("messages") ?: JSONArray()
                    val messages = mutableListOf<ChatMessage>()
                    for (i in 0 until messagesArray.length()) {
                        val msgJson = messagesArray.getJSONObject(i)
                        messages.add(ChatMessage(
                            role = msgJson.optString("role"),
                            content = msgJson.optString("content"),
                            timestamp = msgJson.optString("timestamp")
                        ))
                    }
                    val isSame = messages.size == _chatMessages.value.size &&
                        messages.zip(_chatMessages.value).all { (a, b) ->
                            a.role == b.role && a.content == b.content && a.timestamp == b.timestamp
                        }
                    if (!isSame) {
                        _chatMessages.value = messages
                    }
                }
                "prompt" -> {
                    val promptJson = json.optJSONObject("prompt")
                    if (promptJson != null) {
                        _currentPrompt.value = parsePrompt(promptJson, isPermission = false)
                    } else {
                        _currentPrompt.value = null
                    }
                }
                "permission" -> {
                    val optionsArray = json.optJSONArray("options") ?: JSONArray()
                    val options = parseOptions(optionsArray)
                    _currentPrompt.value = ClaudePrompt(
                        question = json.optString("question"),
                        options = options,
                        timestamp = System.currentTimeMillis().toString(),
                        title = json.optString("tool_name"),
                        context = if (json.has("context")) json.optString("context") else null,
                        requestId = json.optString("request_id"),
                        toolName = json.optString("tool_name"),
                        isPermission = true
                    )
                }
                "permission_resolved" -> {
                    val resolvedId = json.optString("request_id")
                    if (_currentPrompt.value?.requestId == resolvedId) {
                        _currentPrompt.value = null
                    }
                }
                "usage" -> {
                    _contextUsage.value = ContextUsage(
                        totalContext = json.optInt("total_context", 0),
                        contextWindow = json.optInt("context_window", 200000),
                        contextPercent = json.optDouble("context_percent", 0.0).toFloat(),
                        costUsd = json.optDouble("cost_usd", 0.0).toFloat()
                    )
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing message", e)
        }
    }

    private fun parsePrompt(json: JSONObject, isPermission: Boolean): ClaudePrompt {
        val optionsArray = json.optJSONArray("options") ?: JSONArray()
        val options = parseOptions(optionsArray)
        return ClaudePrompt(
            question = json.optString("question"),
            options = options,
            timestamp = json.optString("timestamp"),
            title = if (json.has("title")) json.optString("title") else null,
            context = if (json.has("context")) json.optString("context") else null,
            requestId = if (json.has("request_id")) json.optString("request_id") else null,
            toolName = if (json.has("tool_name")) json.optString("tool_name") else null,
            isPermission = isPermission
        )
    }

    private fun parseOptions(array: JSONArray): List<PromptOption> {
        val options = mutableListOf<PromptOption>()
        for (i in 0 until array.length()) {
            val optJson = array.getJSONObject(i)
            options.add(PromptOption(
                num = optJson.optInt("num"),
                label = optJson.optString("label"),
                description = optJson.optString("description", ""),
                selected = optJson.optBoolean("selected", false)
            ))
        }
        return options
    }
}
