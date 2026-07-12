'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '../../utils/supabase'

export default function Dashboard() {
    const [user, setUser] = useState<any>(null)
    const [subscription, setSubscription] = useState<any>(null)
    const router = useRouter()

    useEffect(() => {
        const fetchUserAndSubscription = async () => {
            const { data: { session } } = await supabase.auth.getSession()

            if (!session) {
                router.push('/')
                return
            }

            setUser(session.user)

            const { data: subData, error } = await supabase
                .from('subscriptions')
                .select('*')
                .eq('user_id', session.user.id)
                .single()

            if (subData) {
                setSubscription(subData)
            } else if (error && error.code !== 'PGRST116') {
                console.error('Error fetching subscription:', error)
            }
        }

        fetchUserAndSubscription()
    }, [router])

    const handleLogout = async () => {
        await supabase.auth.signOut()
        router.push('/')
    }

    if (!user) return (
        <div className="min-h-screen flex items-center justify-center bg-gray-50">
            <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-blue-500"></div>
        </div>
    )

    return (
        <main className="min-h-screen bg-gray-50">
            <nav className="bg-white shadow-sm border-b border-gray-200 px-6 py-4 flex justify-between items-center">
                <h1 className="text-xl font-bold text-blue-600">BasFunds System</h1>
                <button
                    onClick={handleLogout}
                    className="text-sm font-medium text-red-600 hover:text-red-700 hover:bg-red-50 px-4 py-2 rounded-lg transition-colors"
                >
                    ออกจากระบบ
                </button>
            </nav>

            <div className="max-w-4xl mx-auto mt-10 p-6">
                <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-8">
                    <h2 className="text-2xl font-bold text-gray-800 mb-2">ภาพรวมบัญชี (Dashboard)</h2>
                    <p className="text-gray-600 border-b border-gray-100 pb-6 mb-6">
                        เข้าสู่ระบบในชื่อ: <span className="font-semibold text-gray-900">{user.email}</span>
                    </p>

                    <div className="bg-blue-50 text-blue-800 p-4 rounded-lg mb-8">
                        <h3 className="font-semibold mb-3 text-lg border-b border-blue-200 pb-2">สถานะสมาชิกปัจจุบัน</h3>
                        {subscription ? (
                            <div className="space-y-2">
                                <p>แพ็กเกจปัจจุบัน: <span className="font-bold text-xl">{subscription.plan_name}</span></p>
                                <p>สถานะการใช้งาน: <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">{subscription.status}</span></p>
                            </div>
                        ) : (
                            <p className="text-sm">คุณยังไม่มีแพ็กเกจสมาชิกในขณะนี้</p>
                        )}
                    </div>

                    {/* --- ส่วนที่เพิ่มเข้ามาใหม่: ระบบซ่อนคอนเทนต์ --- */}
                    <div>
                        <h2 className="text-xl font-bold text-gray-800 mb-4">เนื้อหาพิเศษสำหรับคุณ (Premium Content)</h2>

                        {/* เช็คว่ามีแพ็กเกจ และสถานะเป็น Active หรือไม่ */}
                        {subscription?.status === 'Active' ? (
                            <div className="bg-gradient-to-r from-green-50 to-emerald-50 border border-green-200 rounded-xl p-6 shadow-sm">
                                <div className="flex items-center gap-3 mb-4">
                                    <span className="text-2xl">✨</span>
                                    <h3 className="text-lg font-bold text-green-800">เครื่องมือวิเคราะห์ Option ขั้นสูง</h3>
                                </div>
                                <p className="text-green-700 mb-4">
                                    ยินดีด้วย! คุณสามารถเข้าถึงเครื่องมือคำนวณและสัญญาณเทรดแบบ Real-time ได้แล้ว
                                </p>
                                <button className="bg-green-600 hover:bg-green-700 text-white font-medium py-2 px-4 rounded-lg transition-colors">
                                    เข้าสู่เครื่องมือวิเคราะห์ (Click)
                                </button>
                            </div>
                        ) : (
                            <div className="bg-gray-50 border border-gray-200 rounded-xl p-6 shadow-sm text-center">
                                <div className="text-4xl mb-3">🔒</div>
                                <h3 className="text-lg font-bold text-gray-700 mb-2">เนื้อหานี้สงวนสิทธิ์เฉพาะสมาชิก Premium</h3>
                                <p className="text-gray-500 mb-4">
                                    อัปเกรดแพ็กเกจของคุณเพื่อปลดล็อกเครื่องมือคำนวณ Option และบทวิเคราะห์เชิงลึก
                                </p>
                                <button
                                    onClick={() => router.push('/pricing')} // เพิ่ม onClick ตรงนี้
                                    className="bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-6 rounded-lg transition-colors shadow-sm"
                                >
                                    ดูแพ็กเกจและราคา (Upgrade)
                                </button>
                            </div>
                        )}
                    </div>
                    {/* ------------------------------------------- */}

                </div>
            </div>
        </main>
    )
}