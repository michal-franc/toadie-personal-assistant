package com.claudewatch.companion.network

import android.util.Log
import com.claudewatch.companion.creature.BackgroundTheme
import com.claudewatch.companion.creature.CreatureMood
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class MessageStatus {
    SENT,       // Successfully sent to server
    PENDING,    // Queued, waiting to send (offline)
    FAILED      // Failed to send, tap to retry
}

data class ChatMessage(
    val id: String = java.util.UUID.randomUUID().toString(),
    val role: String,
    val content: String,
    val timestamp: String,
    val status: MessageStatus = MessageStatus.SENT
)

data class ClaudeState(
    val status: String = "idle",  // idle, listening, thinking, speaking, waiting
    val requestId: String? = null
)

data class ContextUsage(
    val totalContext: Int = 0,
    val contextWindow: Int = 200000,
    val contextPercent: Float = 0f,
    val costUsd: Float = 0f
)

data class MoodUpdate(
    val mood: CreatureMood = CreatureMood.NEUTRAL,
    val background: BackgroundTheme = BackgroundTheme.DEFAULT
)

data class PromptOption(
    val num: Int,
    val label: String,
    val description: String,
    val selected: Boolean
)

data class ClaudePrompt(
    val question: String,
    val options: List<PromptOption>,
    val timestamp: String,
    val title: String? = null,
    val context: String? = null,
    val requestId: String? = null,  // For permission requests
    val toolName: String? = null,   // Tool requesting permission
    val isPermission: Boolean = false
)

enum class ConnectionStatus {
    DISCONNECTED,
    CONNECTING,
    CONNECTED
}

class WebSocketClient(
    private val serverAddress: String
) {
    companion object {
        private const val TAG = "WebSocketClient"
        private const val RECONNECT_DELAY_MS = 5000L
    }

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)  // No timeout for WebSocket
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private var webSocket: WebSocket? = null
    private var reconnectJob: Job? = null
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

    private val _moodUpdate = MutableStateFlow(MoodUpdate())
    val moodUpdate: StateFlow<MoodUpdate> = _moodUpdate

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.i(TAG, "WebSocket connected")
            _connectionStatus.value = ConnectionStatus.CONNECTED
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            Log.d(TAG, "Received: $text")
            handleMessage(text)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "WebSocket closing: $code $reason")
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "WebSocket closed: $code $reason")
            _connectionStatus.value = ConnectionStatus.DISCONNECTED
            scheduleReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.e(TAG, "WebSocket error", t)
            _connectionStatus.value = ConnectionStatus.DISCONNECTED
            scheduleReconnect()
        }
    }

    fun connect() {
        if (_connectionStatus.value == ConnectionStatus.CONNECTING) {
            return
        }

        _connectionStatus.value = ConnectionStatus.CONNECTING
        reconnectJob?.cancel()

        val deviceId = android.os.Build.MODEL ?: "phone"
        val wsUrl = "ws://$serverAddress/ws?device=phone&id=${java.net.URLEncoder.encode(deviceId, "UTF-8")}"
        Log.i(TAG, "Connecting to $wsUrl")

        val request = Request.Builder()
            .url(wsUrl)
            .build()

        webSocket?.close(1000, "Reconnecting")
        webSocket = client.newWebSocket(request, listener)
    }

    fun disconnect() {
        reconnectJob?.cancel()
        webSocket?.close(1000, "User disconnect")
        webSocket = null
        _connectionStatus.value = ConnectionStatus.DISCONNECTED
    }

    fun destroy() {
        disconnect()
        scope.cancel()
        client.dispatcher.executorService.shutdown()
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
                    _chatMessages.value = messages
                }
                "prompt" -> {
                    val promptJson = json.optJSONObject("prompt")
                    if (promptJson != null) {
                        val optionsArray = promptJson.optJSONArray("options") ?: JSONArray()
                        val options = mutableListOf<PromptOption>()
                        for (i in 0 until optionsArray.length()) {
                            val optJson = optionsArray.getJSONObject(i)
                            options.add(PromptOption(
                                num = optJson.optInt("num"),
                                label = optJson.optString("label"),
                                description = optJson.optString("description", ""),
                                selected = optJson.optBoolean("selected", false)
                            ))
                        }
                        _currentPrompt.value = ClaudePrompt(
                            question = promptJson.optString("question"),
                            options = options,
                            timestamp = promptJson.optString("timestamp"),
                            title = if (promptJson.has("title")) promptJson.optString("title") else null,
                            context = if (promptJson.has("context")) promptJson.optString("context") else null
                        )
                    } else {
                        _currentPrompt.value = null
                    }
                }
                "permission" -> {
                    // Permission request from hook - display as prompt
                    val optionsArray = json.optJSONArray("options") ?: JSONArray()
                    val options = mutableListOf<PromptOption>()
                    for (i in 0 until optionsArray.length()) {
                        val optJson = optionsArray.getJSONObject(i)
                        options.add(PromptOption(
                            num = optJson.optInt("num"),
                            label = optJson.optString("label"),
                            description = optJson.optString("description", ""),
                            selected = false
                        ))
                    }
                    _currentPrompt.value = ClaudePrompt(
                        question = json.optString("question"),
                        options = options,
                        timestamp = System.currentTimeMillis().toString(),
                        title = json.optString("tool_name"),
                        context = json.optString("context"),
                        requestId = json.optString("request_id"),
                        toolName = json.optString("tool_name"),
                        isPermission = true
                    )
                }
                "permission_resolved" -> {
                    // Permission was resolved (by this or another client)
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
                "mood" -> {
                    _moodUpdate.value = MoodUpdate(
                        mood = CreatureMood.fromString(json.optString("mood", "neutral")),
                        background = BackgroundTheme.fromString(json.optString("background", "default"))
                    )
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing message", e)
        }
    }
}
