import { NextResponse } from 'next/server';
import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);
export async function POST(request: Request) {
    try {
        // 1. รับข้อมูล Price ID และ User ID ที่หน้าเว็บส่งมา
        const { priceId, userId } = await request.json();

        if (!priceId || !userId) {
            return NextResponse.json({ error: 'Missing priceId or userId' }, { status: 400 });
        }

        // 2. สั่ง Stripe ให้สร้างหน้าต่างจ่ายเงิน (Checkout Session)
        const session = await stripe.checkout.sessions.create({
            payment_method_types: ['card'], // รับบัตรเครดิต/เดบิต
            line_items: [
                {
                    price: priceId, // รหัสแพ็กเกจที่คุณเลือก
                    quantity: 1,
                },
            ],
            mode: 'subscription', // โหมดเก็บเงินรายเดือน

            // ถ้าลูกค้าจ่ายเงินสำเร็จ ให้ Stripe พากลับมาหน้านี้:
            success_url: `http://localhost:3000/dashboard?success=true`,

            // ถ้าลูกค้ายกเลิกการจ่ายเงิน ให้พากลับมาหน้านี้:
            cancel_url: `http://localhost:3000/pricing?canceled=true`,

            // แนบรหัสลูกค้าไปด้วย เพื่อใช้เช็คตอนอัปเดตฐานข้อมูล
            client_reference_id: userId,
        });

        // 3. ส่ง URL ของหน้าจ่ายเงินกลับไปให้หน้าบ้าน
        return NextResponse.json({ url: session.url });

    } catch (error: any) {
        console.error('Stripe error:', error);
        return NextResponse.json({ error: error.message }, { status: 500 });
    }
}