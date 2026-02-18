'use client';

import { Canvas } from '@react-three/fiber';
import { Float, Line, OrbitControls, Sparkles } from '@react-three/drei';

function OrbitRing({ radius, color }: { radius: number; color: string }) {
  const points = Array.from({ length: 121 }, (_, idx) => {
    const t = (idx / 120) * Math.PI * 2;
    return [Math.cos(t) * radius, Math.sin(t) * radius * 0.55, 0] as [number, number, number];
  });

  return <Line points={points} color={color} lineWidth={0.6} transparent opacity={0.45} />;
}

export function HeroScene() {
  return (
    <Canvas camera={{ position: [0, 0, 4.4], fov: 46 }}>
      <color attach="background" args={['#0a1020']} />
      <ambientLight intensity={1.1} />
      <pointLight position={[2.4, 2.6, 1.4]} intensity={1.7} color="#6DF2D8" />
      <pointLight position={[-2.2, -2.4, 1.4]} intensity={0.9} color="#5F7AFF" />

      <group rotation={[-0.2, 0.35, 0]}>
        <Float speed={1.4} rotationIntensity={0.7} floatIntensity={0.65}>
          <mesh>
            <icosahedronGeometry args={[1.12, 1]} />
            <meshStandardMaterial color="#53EED1" wireframe transparent opacity={0.7} />
          </mesh>
        </Float>

        <OrbitRing radius={1.55} color="#53EED1" />
        <group rotation={[0.55, 0, 0.52]}>
          <OrbitRing radius={1.95} color="#6C86FF" />
        </group>
      </group>

      <Sparkles count={46} size={2.3} speed={0.22} color="#9EF8E6" scale={[6, 3.2, 2]} />
      <OrbitControls enableZoom={false} enablePan={false} autoRotate autoRotateSpeed={0.35} />
    </Canvas>
  );
}
