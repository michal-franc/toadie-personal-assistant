package com.claudewatch.companion.creature

import android.animation.ArgbEvaluator
import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator

class BackgroundView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val paint = Paint()
    private var topColor = 0xFF1a1a2e.toInt()
    private var bottomColor = 0xFF0d0d1a.toInt()
    private var themeAnimator: ValueAnimator? = null

    fun setTheme(theme: BackgroundTheme) {
        val (newTop, newBottom) = when (theme) {
            BackgroundTheme.DEFAULT -> Pair(0xFF1a1a2e.toInt(), 0xFF0d0d1a.toInt())
            BackgroundTheme.WARM -> Pair(0xFF2e1a1a.toInt(), 0xFF1a0d0d.toInt())
            BackgroundTheme.COOL -> Pair(0xFF1a1a2e.toInt(), 0xFF0d1a2e.toInt())
            BackgroundTheme.NATURE -> Pair(0xFF1a2e1a.toInt(), 0xFF0d1a0d.toInt())
            BackgroundTheme.ELECTRIC -> Pair(0xFF2e1a2e.toInt(), 0xFF0d1a2e.toInt())
        }

        val startTop = topColor
        val startBottom = bottomColor
        val evaluator = ArgbEvaluator()

        themeAnimator?.cancel()
        themeAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 800
            interpolator = AccelerateDecelerateInterpolator()
            addUpdateListener { animator ->
                val t = animator.animatedValue as Float
                topColor = evaluator.evaluate(t, startTop, newTop) as Int
                bottomColor = evaluator.evaluate(t, startBottom, newBottom) as Int
                invalidate()
            }
            start()
        }
    }

    override fun onDraw(canvas: Canvas) {
        paint.shader = LinearGradient(
            0f, 0f, 0f, height.toFloat(),
            topColor, bottomColor,
            Shader.TileMode.CLAMP
        )
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), paint)
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        themeAnimator?.cancel()
    }
}
