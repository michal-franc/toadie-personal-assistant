package com.claudewatch.app.chat

import android.graphics.Rect
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.claudewatch.app.R
import com.claudewatch.app.network.ChatMessage

class WatchChatAdapter : ListAdapter<ChatMessage, WatchChatAdapter.ChatViewHolder>(DIFF_CALLBACK) {

    companion object {
        private const val VIEW_TYPE_USER = 0
        private const val VIEW_TYPE_CLAUDE = 1
        private const val VIEW_TYPE_THINKING = 2

        private const val THINKING_ID = "__thinking__"

        /** Negative vertical spacing (in dp) to create overlapping bubbles. */
        const val OVERLAP_DP = -6

        private val DIFF_CALLBACK = object : DiffUtil.ItemCallback<ChatMessage>() {
            override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
                return oldItem.id == newItem.id
            }

            override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
                return oldItem == newItem
            }
        }
    }

    private var showThinking = false
    private var baseMessages: List<ChatMessage> = emptyList()

    fun setThinking(thinking: Boolean) {
        if (showThinking == thinking) return
        showThinking = thinking
        rebuildList()
    }

    fun submitMessages(messages: List<ChatMessage>, commitCallback: Runnable? = null) {
        baseMessages = messages
        rebuildList(commitCallback)
    }

    private fun rebuildList(commitCallback: Runnable? = null) {
        val list = if (showThinking) {
            baseMessages + ChatMessage(
                id = THINKING_ID,
                role = "thinking",
                content = "",
                timestamp = ""
            )
        } else {
            baseMessages
        }
        submitList(list, commitCallback)
    }

    override fun getItemViewType(position: Int): Int {
        val item = getItem(position)
        return when (item.role) {
            "user" -> VIEW_TYPE_USER
            "thinking" -> VIEW_TYPE_THINKING
            else -> VIEW_TYPE_CLAUDE
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ChatViewHolder {
        val layoutId = when (viewType) {
            VIEW_TYPE_USER -> R.layout.item_watch_chat_user
            VIEW_TYPE_THINKING -> R.layout.item_watch_chat_thinking
            else -> R.layout.item_watch_chat_claude
        }

        val view = LayoutInflater.from(parent.context).inflate(layoutId, parent, false)
        return ChatViewHolder(view)
    }

    override fun onBindViewHolder(holder: ChatViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    class ChatViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val messageText: TextView = itemView.findViewById(R.id.messageText)

        fun bind(message: ChatMessage) {
            if (message.role == "thinking") {
                // Text is already set in XML
                return
            }
            messageText.text = message.content
        }
    }

    /**
     * ItemDecoration that adds negative vertical spacing between chat items
     * so bubbles overlap like a dense chat layout.
     */
    class OverlapDecoration : RecyclerView.ItemDecoration() {
        override fun getItemOffsets(outRect: Rect, view: View, parent: RecyclerView, state: RecyclerView.State) {
            val position = parent.getChildAdapterPosition(view)
            if (position > 0) {
                val density = view.resources.displayMetrics.density
                outRect.top = (OVERLAP_DP * density).toInt()
            }
        }
    }
}
