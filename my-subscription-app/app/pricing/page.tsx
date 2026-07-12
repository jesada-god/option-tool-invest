'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '../../utils/supabase'

export default function Pricing() {
  const [user, setUser] = useState<any>(null)
  const [isLoading, setIsLoading] = useState(false)
  const router = useRouter()

  useEffect(() => {
    const checkUser = async () => {
      const { data: { session } } = await supabase.auth.getSession()
      if (!session) {
        router.push('/')
      } else {
        setUser(session.user)
      }
    }
    checkUser()
  }, [router])

  const handleSubscribe = async (priceId: string) => {
    if (!user) return
    setIsLoading(true)

    try {
      const response = await fetch('/api/checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          priceId: priceId,
          userId: user.id,
        }),
      })

      const data = await response.json()

      if (data.url) {
        window.location.href = data.url
      } else {
        alert('เกิดข้อผิดพลาด: ' + data.error)
        setIsLoading(false)
      }
    } catch (error) {
      console.error('Error:', error)
      alert('ไม่สามารถเชื่อมต่อระบบชำระเงินได้')
      setIsLoading(false)
    }
  }

  if (!user) return <div className="min-h-screen flex items-center justify-center"><div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-blue-500"></div></div>

  return (
    <main className="min-h-screen bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-7xl mx-auto text-center">
        <h2 className="text-3xl font-extrabold text-gray-900 sm:text-5xl">เลือกแพ็กเกจที่เหมาะกับคุณ</h2>
        <p className="mt-4 text-xl text-gray-500">ปลดล็อกเครื่องมือวิเคราะห์ Option เพื่อการลงทุนที่ดีกว่า</p>
      </div>

      <div className="mt-16 max-w-5xl mx-auto grid gap-8 lg:grid-cols-2 lg:gap-12">
        {/* กล่องแพ็กเกจ Basic */}
        <div className="bg-white rounded-2xl shadow-sm border border-gray-200 p-8 flex flex-col">
          <h3 className="text-2xl font-semibold text-gray-900 text-center">Basic Plan</h3>
          <p className="mt-4 text-center text-gray-500">เริ่มต้นใช้งานเครื่องมือพื้นฐาน</p>
          <div className="mt-6 text-center text-5xl font-extrabold text-gray-900">฿499<span className="text-xl font-medium text-gray-500">/เดือน</span></div>
          <ul className="mt-8 space-y-4 flex-1">
            <li className="flex items-center"><span className="text-green-500 mr-3">✓</span>ดูราคา Option แบบ Real-time</li>
            <li className="flex items-center"><span className="text-green-500 mr-3">✓</span>กราฟเทคนิคพื้นฐาน</li>
          </ul>
          <button 
            onClick={() => handleSubscribe('price_1TsQhyChcdvHBA4uaa3itx5H')}
            disabled={isLoading}
            className="mt-8 w-full bg-blue-50 text-blue-700 hover:bg-blue-100 font-bold py-3 px-4 rounded-xl transition-colors"
          >
            {isLoading ? 'กำลังพาท่านไปหน้าชำระเงิน...' : 'สมัครสมาชิกแพ็กเกจนี้'}
          </button>
        </div>

        {/* กล่องแพ็กเกจ Premium Pro */}
        <div className="bg-blue-600 rounded-2xl shadow-xl border border-blue-600 p-8 flex flex-col relative transform lg:-translate-y-4">
          <div className="absolute top-0 right-0 -translate-y-1/2 translate-x-1/4">
            <span className="bg-gradient-to-r from-pink-500 to-orange-400 text-white text-xs font-bold uppercase tracking-wider py-1 px-3 rounded-full shadow-sm">ยอดฮิต</span>
          </div>
          <h3 className="text-2xl font-semibold text-white text-center">Premium Pro</h3>
          <p className="mt-4 text-center text-blue-100">จัดเต็มทุกเครื่องมือวิเคราะห์ขั้นสูง</p>
          <div className="mt-6 text-center text-5xl font-extrabold text-white">฿999<span className="text-xl font-medium text-blue-200">/เดือน</span></div>
          <ul className="mt-8 space-y-4 flex-1 text-white">
            <li className="flex items-center"><span className="text-blue-300 mr-3">✓</span>ดูราคา Option แบบ Real-time</li>
            <li className="flex items-center"><span className="text-blue-300 mr-3">✓</span>กราฟเทคนิคขั้นสูง</li>
            <li className="flex items-center"><span className="text-blue-300 mr-3">✓</span>ระบบแจ้งเตือนจุดเข้าซื้อ-ขาย (Signals)</li>
          </ul>
          <button 
            onClick={() => handleSubscribe('price_1TsQkhChcdvHBA4uDuaUBVkr')}
            disabled={isLoading}
            className="mt-8 w-full bg-white text-blue-600 hover:bg-gray-50 font-bold py-3 px-4 rounded-xl transition-colors shadow-sm"
          >
            {isLoading ? 'กำลังพาท่านไปหน้าชำระเงิน...' : 'สมัครสมาชิกแพ็กเกจนี้'}
          </button>
        </div>
      </div>
    </main>
  )
}